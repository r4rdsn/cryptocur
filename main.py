import config
from db import database
from api import Market

import json
import flask
import random
import logging
from threading import Timer

import telebot
from telebot import logger, console_output_handler
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update


logger.setLevel(logging.INFO)
logger.removeHandler(console_output_handler)
new_output_handler = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s.%(msecs).03d [%(levelname)-7s] %(message)s", datefmt="%H:%M:%S")
new_output_handler.setFormatter(formatter)
logger.addHandler(new_output_handler)

market = Market()


class Notificator:
    FILENAME = "currencies.json"

    def __init__(self):
        self.notification_loop()

    def notification_loop(self):
        market.update_currencies_raw()
        subscribers = database.users.find()

        currencies_data = self.load_data()
        iter_cache = {}
        for user in subscribers:
            relevant_currencies = []
            for cur in user["subscriptions"]:
                if cur in market.currencies_list:
                    if cur in iter_cache:
                        stat = iter_cache[cur]["result"]
                    else:
                        try:
                            currency_ticker = market.bittrex.get_ticker(cur)
                            iter_cache[cur] = currency_ticker
                            stat = currency_ticker["result"]
                        except ValueError:
                            continue
                else:
                    continue

                prev_price = currencies_data.get(cur, {"Last": None})["Last"]
                currencies_data[cur] = stat
                if stat["Last"] != prev_price:
                    difference = (stat["Last"] - (prev_price if prev_price else 0)) / stat["Last"]
                    if difference and abs(difference) >= user["change"]:
                        relevant_currencies.append((cur, stat["Last"], difference))

            if relevant_currencies:
                message = "Изменились цены на следующие валюты:\n" + "\n".join(
                    ["{} - {:.8f} {}{:.8f}%".format(name, price, "⬇" if diff < 0 else "⬆", abs(diff * 100))
                        for name, price, diff in relevant_currencies]
                )
                bot.send_message(int(user["id"]), message)

        self.dump_data(currencies_data)

        self.timer = Timer(config.DELAY, self.notification_loop)
        self.timer.daemon = True
        self.timer.start()

    def end_loop(self):
        self.timer.cancel()

    def dump_data(self, data):
        with open(self.FILENAME, 'w') as outfile:
            json.dump(data, outfile)

    def load_data(self):
        with open(self.FILENAME, 'r') as infile:
            return json.load(infile)


bot = telebot.TeleBot(config.TOKEN)


@bot.message_handler(commands=["help"])
def help_message(message):
    bot.send_message(message.chat.id, '💰 Привет! Я - криптовалютный бот.\n📇 Введите команду "/list", чтобы увидеть доступные для подписки криптовалюты.\n✔ Вы также можете ввести пару криптовалют вручную в формате "BTC-LTC" для подписки.\n❌ Повторная подписка отменяет её.\n📋 Введите команду "/subs" для просмотра списка ваших подписок.\n💯 Введите команду "/change" для просмотра соотношения изменения, при котором я буду вас оповещать. При вводе этой команды с аргументом в форме десятичной дроби вы сможете изменить это соотношение.')


@bot.message_handler(commands=["start"])
def start_message(message):
    if not database.users.find_one({"id": message.from_user.id}):
        database.users.insert_one({"id": message.from_user.id, "subscriptions": [], "change": config.CHANGE})
        help_message(message)
    else:
        bot.send_message(message.chat.id, 'Привет! Мы уже знакомы! Введите команду "/help" для просмотра доступных команд.')


@bot.message_handler(commands=["change"])
def change_difference(message):
    args = message.text.split()
    if len(args) == 1:
        user = database.users.find_one({"id": message.from_user.id})
        bot.send_message(message.chat.id, 'Ваше отношение: {:.8f}'.format(user["change"]))
    else:
        user_arg = args[1]
        try:
            change = float(user_arg.replace(',', '.'))
            database.users.update_one({"id": message.from_user.id}, {"$set": {"change": change}})
            bot.send_message(message.chat.id, 'Ваше соотношение изменено.')
        except ValueError:
            bot.send_message(message.chat.id, 'Ошибка при получении отношения. Убедитесь, что оно выражено в десятичной дроби.')


def currencies_list_keyboard(page_number):
    page_number = int(page_number)
    size = 5
    i = page_number * size + bool(page_number)
    currencies_count = len(market.currencies_list)
    if 0 <= i < currencies_count:
        inline_keyboard = InlineKeyboardMarkup(row_width=1)
        inline_keyboard.add(
            *[InlineKeyboardButton(text=c, callback_data="currency %s" % c) for c in market.currencies_list[i:i + size]]
        )

        control_buttons = []
        if page_number != 0:
            control_buttons.append(
                InlineKeyboardButton(text="<<<", callback_data="page %d" % (page_number - 1))
            )
        if i + size < currencies_count:
            control_buttons.append(
                InlineKeyboardButton(text=">>>", callback_data="page %d" % (page_number + 1))
            )

        inline_keyboard.row(*control_buttons)
        return inline_keyboard


@bot.callback_query_handler(func=lambda call: call.data.split()[0] == "currency")
def currency_call_subscription(call):
    user_document = database.users.find_one({"id": call.from_user.id})
    user_subscriptions = user_document["subscriptions"]
    currency_key = call.data.split()[1]
    if currency_key in market.currencies_list:
        if currency_key not in user_subscriptions:
            database.users.update_one({"_id": user_document["_id"]}, {"$push": {"subscriptions": currency_key}})
            bot.answer_callback_query(
                callback_query_id=call.id,
                show_alert=True,
                text="✔ Вы подписались на изменения цены криптовалюты {}".format(currency_key)
            )
        else:
            database.users.update_one({"_id": user_document["_id"]}, {"$pull": {"subscriptions": currency_key}})
            bot.answer_callback_query(
                callback_query_id=call.id,
                show_alert=True,
                text="❌ Вы отменили подписку на изменения цены криптовалюты {}".format(currency_key)
            )
    else:
        answer_remaining(call.message)


@bot.message_handler(regexp="[A-Za-z0-9]{1,6}(?:_| |-)[A-Za-z0-9]{1,6}")
def currency_message_subscription(message):
    user_document = database.users.find_one({"id": message.from_user.id})
    user_subscriptions = user_document["subscriptions"]
    currency_key = message.text.replace(" ", "-").replace("_", "-").upper()
    if currency_key in market.currencies_list:
        if currency_key not in user_subscriptions:
            database.users.update_one({"_id": user_document["_id"]}, {"$push": {"subscriptions": currency_key}})
            bot.send_message(
                chat_id=message.chat.id,
                text="✔ Вы подписались на изменения цены криптовалюты {}".format(currency_key)
            )
        else:
            database.users.update_one({"_id": user_document["_id"]}, {"$pull": {"subscriptions": currency_key}})
            bot.send_message(
                chat_id=message.chat.id,
                text="❌ Вы отменили подписку на изменения цены криптовалюты {}".format(currency_key)
            )
    else:
        answer_remaining(message)


@bot.callback_query_handler(func=lambda call: call.data.split()[0] == "page")
def callback_inline(call):
    page_number = call.data.split()[1]
    new_markup = currencies_list_keyboard(page_number)
    if new_markup is not None:
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=new_markup)
        except:
            bot.answer_callback_query(callback_query_id=call.id, text="Не надо меня так теребить! Я нервничаю!")
    else:
        bot.answer_callback_query(callback_query_id=call.id)


@bot.message_handler(func=lambda m: m.text == "Список криптовалют")
@bot.message_handler(commands=["list"])
def show_currency_list(message):
    bot.send_message(message.chat.id, "Выберите криптовалюту", reply_markup=currencies_list_keyboard(0))


@bot.message_handler(commands=["subs"])
def show_user_subscriptions(message):
    user_document = database.users.find_one({"id": message.from_user.id})
    user_subscriptions = user_document["subscriptions"]
    if user_subscriptions:
        bot.send_message(message.chat.id, "Ваши подписки: " + ", ".join(user_subscriptions))
    else:
        bot.send_message(message.chat.id, "Список ваших подписок пуст.")


@bot.message_handler(func=lambda _: True)
def answer_remaining(message):
    bot.send_message(message.chat.id, 'Я вас не понял. Введите команду "/help" для просмотра доступных команд.')


app = flask.Flask(__name__)


@app.route('/', methods=["GET", "HEAD"])
def index():
    return ""


@app.route(config.WEBHOOK_URL_PATH, methods=["POST"])
def webhook():
    if flask.request.headers.get("content-type") == "application/json":
        json_string = flask.request.get_data().decode('utf-8')
        update = Update.de_json(json_string)
        bot.process_new_updates([update])
        return ""
    else:
        flask.abort(403)


def main():
    notificator = Notificator()
    bot.remove_webhook()

    bot.set_webhook(url=config.WEBHOOK_URL_BASE + config.WEBHOOK_URL_PATH,
                    certificate=open(config.WEBHOOK_SSL_CERT, 'r'))
    app.run(host=config.WEBHOOK_LISTEN,
            port=config.WEBHOOK_PORT,
            ssl_context=(config.WEBHOOK_SSL_CERT, config.WEBHOOK_SSL_PRIV),
            debug=True)

    notificator.end_loop()


if __name__ == "__main__":
    main()
