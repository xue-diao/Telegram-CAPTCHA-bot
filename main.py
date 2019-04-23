import json
import sched
import logging
import threading
from time import time, sleep
from challenge import Challenge
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import Updater, MessageHandler, CallbackQueryHandler, Filters
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

config, config_lock = dict(), threading.Lock()
updater = None
dispatcher = None
# Key: chat_id + '|' + user_id + '|' + msg_id
# Value: (challenge object, event object)
current_challenges, cch_lock = dict(), threading.Lock()
challenge_sched = sched.scheduler(time, sleep)


def load_config():
    global config
    config_lock.acquire()
    with open('config.json', encoding='utf-8') as f:
        config = json.load(f)
    config_lock.release()


def save_config():
    config_lock.acquire()
    with open('config.json', 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4)
    config_lock.release()


def challenge_user(bot, update):
    global config, current_challenges

    msg = update.message
    if not msg.new_chat_members:
        return None
    target = msg.new_chat_members[0]
    # Invited by others
    if msg.from_user != target:
        if bot.get_me() in msg.new_chat_members:
            config_lock.acquire()
            group_config = config.get(str(msg.chat.id), config['*'])
            bot.send_message(chat_id=msg.chat.id,
                text=group_config['msg_self_introduction'])
            config_lock.release()
        return None

    # Attempt to restrict the user
    try:
        bot.restrict_chat_member(msg.chat.id, msg.from_user.id)
    except TelegramError:
        # maybe the bot does not have the privilege, so skip
        return None

    config_lock.acquire()
    group_config = config.get(str(msg.chat.id), config['*'])
    config_lock.release()

    challenge = Challenge()

    def challenge_to_buttons(ch):
        choices = [[InlineKeyboardButton(str(c), callback_data=str(c))]
            for c in ch.choices()]
        # manual approval/refusal by group admins
        return choices + [[InlineKeyboardButton(
            group_config['msg_approve_manually'], callback_data='+'),
        InlineKeyboardButton(group_config['msg_refuse_manually'],
            callback_data='-')]]

    timeout = group_config['challenge_timeout']

    bot_msg = bot.send_message(chat_id=msg.chat.id,
        text=group_config['msg_challenge'].format(
            timeout=timeout, challenge=challenge.qus()),
        reply_to_message_id=msg.message_id,
        reply_markup=InlineKeyboardMarkup(challenge_to_buttons(challenge)))

    timeout_event = challenge_sched.enter(group_config['challenge_timeout'],
        10, handle_challenge_timeout,
        argument=(bot, msg.chat.id, msg.from_user.id, bot_msg.message_id))

    cch_lock.acquire()
    current_challenges['{chat}|{msg}'.format(
        chat=msg.chat.id,
        msg=bot_msg.message_id)] = (challenge, msg.from_user.id, timeout_event)
    cch_lock.release()


def handle_challenge_timeout(bot, chat, user, bot_msg):
    global config, current_challenges

    config_lock.acquire()
    group_config = config.get(str(chat), config['*'])
    config_lock.release()

    cch_lock.acquire()
    del current_challenges['{chat}|{msg}'.format(chat=chat, msg=bot_msg)]
    cch_lock.release()

    try:
        bot.edit_message_text(group_config['msg_challenge_failed'],
            chat_id=chat, message_id=bot_msg, reply_markup=None)
    except TelegramError:
        # it is very possible that the message has been deleted
        # so assume the case has been dealt by group admins, simply ignore it
        return None

    if group_config['challenge_timeout_action'] == 'ban':
        bot.kick_chat_member(chat, user)
    else:  # restrict
        # assume that the user is already restricted (when joining the group)
        pass

    if group_config['delete_failed_challenge']:
        challenge_sched.enter(group_config['delete_failed_challenge_interval'],
            1, bot.delete_message, argument=(chat, bot_msg))


def handle_challenge_response(bot, update):
    global config, current_challenges

    query = update['callback_query']
    user_ans = query['data']

    chat = update.effective_chat.id
    user = update.effective_user.id
    username = update.effective_user.name
    bot_msg = update.effective_message.message_id

    config_lock.acquire()
    group_config = config.get(str(chat), config['*'])
    config_lock.release()

    # handle manual approval/refusal by group admins
    if query['data'] in ['+', '-']:
        admins = bot.get_chat_administrators(chat)
        # the creator case must be special judged
        if not any([admin.user.id == user and (admin.can_restrict_members or admin.status == 'creator') for admin in admins]):
            bot.answer_callback_query(callback_query_id=query['id'],
                text=group_config['msg_permission_denied'])
            return None

        ch_id = '{chat}|{msg}'.format(chat=chat, msg=bot_msg)
        cch_lock.acquire()
        challenge, target, timeout_event = current_challenges.get(ch_id, (None, None, None))
        del current_challenges[ch_id]
        cch_lock.release()
        challenge_sched.cancel(timeout_event)

        if query['data'] == '+':
            # lift the restriction
            try:
                bot.restrict_chat_member(chat, target,
                    can_send_messages=True, can_send_media_messages=False,
                    can_send_other_messages=False, can_add_web_page_previews=False)
            except TelegramError:
                bot.answer_callback_query(callback_query_id=query['id'],
                    text=group_config['msg_bot_no_permission'])
                return None
            bot.edit_message_text(group_config['msg_approved'].format(user=username),
                chat_id=chat, message_id=bot_msg, reply_mark=None)
        else:  # query['data'] == '-'
            try:
                bot.kick_chat_member(chat, target)
            except TelegramError:
                bot.answer_callback_query(callback_query_id=query['id'],
                    text=group_config['msg_bot_no_permission'])
                return None
            bot.edit_message_text(group_config['msg_refused'].format(user=username),
                chat_id=chat, message_id=bot_msg, reply_mark=None)

        bot.answer_callback_query(callback_query_id=query['id'])

        return None


    ch_id = '{chat}|{msg}'.format(chat=chat, msg=bot_msg)
    cch_lock.acquire()
    challenge, target, timeout_event = current_challenges.get(ch_id, (None, None, None))
    cch_lock.release()

    if user != target:
        bot.answer_callback_query(callback_query_id=query['id'],
            text=group_config['msg_challenge_not_for_you'])
        return None

    challenge_sched.cancel(timeout_event)

    cch_lock.acquire()
    del current_challenges[ch_id]
    cch_lock.release()

    # lift the restriction
    try:
        bot.restrict_chat_member(chat, target,
            can_send_messages=True, can_send_media_messages=False,
            can_send_other_messages=False, can_add_web_page_previews=False)
    except TelegramError:
        # This my happen when the bot is deop-ed after the user join
        # and before the user click the button
        # TODO: design messages for this occation
        pass

    bot.answer_callback_query(callback_query_id=query['id'])

    # verify the ans
    correct = (str(challenge.ans()) == query['data'])
    msg = 'msg_challenge_passed' if correct else 'msg_challenge_mercy_passed'
    bot.edit_message_text(group_config[msg],
        chat_id=chat, message_id=bot_msg, reply_mark=None)

    if group_config['delete_passed_challenge']:
        challenge_sched.enter(group_config['delete_passed_challenge_interval'],
            5, bot.delete_message, argument=(chat, bot_msg))


def main():
    global updater, dispatcher

    load_config()
    updater = Updater(config['token'])
    dispatcher = updater.dispatcher

    challenge_handler = MessageHandler(Filters.status_update.new_chat_members,
        challenge_user)
    callback_handler = CallbackQueryHandler(handle_challenge_response)
    dispatcher.add_handler(challenge_handler)
    dispatcher.add_handler(callback_handler)

    updater.start_polling()

    def run_sched():
        while True:
            challenge_sched.run(blocking=False)
            sleep(1)

    threading.Thread(target=run_sched, name="run_challenge_sched").start()
    while True:
        try:
            sleep(600)
        except KeyboardInterrupt:
            save_config()
            exit(0)


if __name__ == '__main__':
    main()
