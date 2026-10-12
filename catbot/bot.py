"""Telegram-часть: команды, кнопки и периодический «тик», который рассылает напоминания."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import texts
from .config import Config, Task
from .ration import Need, daily_need, due_milestones
from .scheduler import (
    Action,
    Existing,
    Instance,
    day_mode,
    decide,
    home_pull,
    instances_for_day,
    next_assignee,
    plan_sync,
)
from .storage import Record, Store

log = logging.getLogger(__name__)

BTN_HOME = "🏠 Я дома"
BTN_TODAY = "📋 Сегодня"
BTN_ALERT = "🚨 Что-то не так"
MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_HOME), KeyboardButton(BTN_TODAY)], [KeyboardButton(BTN_ALERT)]],
    resize_keyboard=True,
    is_persistent=True,
)

COMMANDS = [
    BotCommand("today", "План на сегодня"),
    BotCommand("home", "Я дома — что нужно сделать?"),
    BotCommand("alert", "Что-то не так с кошкой — сообщить родителям"),
    BotCommand("ration", "Порции и расчёт нормы"),
    BotCommand("weight", "Записать вес кошки"),
    BotCommand("plan", "Правила кормления"),
    BotCommand("mode", "Режим дня (родители)"),
    BotCommand("pause", "Пауза напоминаний (родители)"),
    BotCommand("resume", "Снять паузу (родители)"),
    BotCommand("help", "Справка"),
]

# Результат отправки: доставлено / не доставить никогда (нет чата, бот заблокирован) / временный сбой сети.
OK, PERMANENT, TRANSIENT = "ok", "permanent", "transient"
MILESTONES_FROM = time(9, 0)  # не будить родителей ночными сообщениями о возрасте
WEIGHT_JUMP = 0.25  # изменение веса больше чем на 25% — переспросить


def settled(results: list[str]) -> bool:
    """Можно считать рассылку состоявшейся: кто-то получил или повторять бессмысленно."""
    return OK in results or TRANSIENT not in results


class CatBot:
    def __init__(self, cfg: Config, store: Store, plan_text: str = ""):
        self.cfg = cfg
        self.store = store
        self.plan_text = plan_text
        # Тик и кнопки не должны одновременно слать одно и то же напоминание.
        self.lock = asyncio.Lock()

    # --- вспомогательное ---

    def now(self) -> datetime:
        return datetime.now(self.cfg.timezone)

    def name(self, member_id: str | None) -> str:
        return texts._name(self.cfg, member_id)

    def needs(self, day: date) -> dict[str, Need]:
        return {c.id: daily_need(self.cfg, c, day, self.store.latest_weight(c.id)) for c in self.cfg.cats}

    def task_of(self, rec: Record) -> Task | None:
        try:
            return self.cfg.task(rec.task_id)
        except KeyError:
            return None  # задачу убрали из конфига

    def instance_of(self, rec: Record) -> Instance | None:
        task = self.task_of(rec)
        return Instance(rec.id, task, rec.day, rec.due_at) if task else None

    def instance(self, instance_id: str) -> Instance | None:
        rec = self.store.get(instance_id)
        return self.instance_of(rec) if rec else None

    async def sync_day(self, app: Application, day: date, now: datetime) -> tuple[str, list[Instance]]:
        """Приводит задачи дня в базе к текущему плану (режим дня, конфиг) — см. scheduler.plan_sync."""
        override = self.store.day_mode(day)
        mode = day_mode(self.cfg, day, override)
        insts = instances_for_day(self.cfg, day, override)
        records = [
            Existing(r.id, r.due_at, r.status, r.sent_count, bool(t and t.kind == "feeding"), t is not None)
            for r, t in ((r, self.task_of(r)) for r in self.store.for_day(day))
        ]
        plan = plan_sync(insts, records, now)
        self.store.delete_instances(plan.delete)
        self.store.set_status(plan.revive, "pending")
        self.store.set_status(plan.cancel, "skipped")
        self.store.ensure_instances(plan.create)
        self.store.ensure_instances(plan.create_now, adjusted=True)
        self.store.ensure_instances(plan.create_skipped, status="skipped")
        self.store.sync_due(insts)
        for instance_id in plan.cancel:
            await self.close_messages(app, instance_id, "⏭ Отменено: сменился план дня.")
        return mode, insts

    def paused(self) -> bool:
        return self.store.get_kv("paused") == "1"

    def keyboard(self, instance_id: str) -> InlineKeyboardMarkup:
        """Кнопки для ответственного."""
        snooze = self.cfg.reminders.snooze_min
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Готово", callback_data=f"done|{instance_id}")],
                [
                    InlineKeyboardButton(f"⏰ Через {snooze} мин", callback_data=f"snooze|{instance_id}"),
                    InlineKeyboardButton("🙅 Не могу", callback_data=f"pass|{instance_id}"),
                ],
            ]
        )

    @staticmethod
    def done_keyboard(instance_id: str) -> InlineKeyboardMarkup:
        """Кнопка для тех, кому пришла эскалация: сделать самому можно, отложить чужую задачу — нет."""
        return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Готово (сделал(а) сам(а))", callback_data=f"done|{instance_id}")]])

    @staticmethod
    def leftovers_keyboard(instance_id: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("👍 Всё съели", callback_data=f"left|ok|{instance_id}"),
                InlineKeyboardButton("🍽 Осталось много", callback_data=f"left|many|{instance_id}"),
            ]]
        )

    def member_of(self, update: Update) -> str | None:
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            return None
        return self.store.member_by_chat(chat.id)

    def is_parent(self, member_id: str | None) -> bool:
        if not member_id:
            return False
        try:
            return self.cfg.member(member_id).is_parent
        except KeyError:
            return False

    def escalation_targets(self, task: Task, assignee: str) -> list[str]:
        targets = []
        if task.backup and task.backup != assignee:
            targets.append(task.backup)
        targets += [m.id for m in self.cfg.parents if m.id not in targets and m.id != assignee]
        return targets

    async def send(
        self, app: Application, member_id: str, text: str, markup=None, instance_id: str | None = None, silent: bool = False
    ) -> str:
        chat_id = self.store.chat_of(member_id)
        if chat_id is None:
            return PERMANENT
        try:
            msg = await app.bot.send_message(
                chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_notification=silent
            )
        except Forbidden:
            log.warning("Участник %s заблокировал бота", member_id)
            return PERMANENT
        except BadRequest:  # подкласс NetworkError, но повтор не поможет
            log.exception("Telegram отклонил сообщение для %s", member_id)
            return PERMANENT
        except (NetworkError, RetryAfter):
            log.warning("Сбой сети при отправке %s — повторю на следующем тике", member_id)
            return TRANSIENT
        except TelegramError:
            log.exception("Не удалось отправить сообщение %s", member_id)
            return PERMANENT
        if instance_id:
            self.store.add_message(instance_id, chat_id, msg.message_id)
        return OK

    async def send_many(self, app: Application, members: list[str], text: str, markup=None, instance_id=None, silent=False) -> list[str]:
        return [await self.send(app, m, text, markup, instance_id, silent) for m in members]

    async def send_parents(self, app: Application, text: str, exclude: set[str] = frozenset(), markup=None, instance_id=None, silent=False) -> list[str]:
        members = [m.id for m in self.cfg.parents if m.id not in exclude]
        return await self.send_many(app, members, text, markup, instance_id, silent)

    # --- тик ---

    async def tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.run_tick(context.application, self.now())
        except Exception:
            log.exception("Ошибка в тике")

    async def run_tick(self, app: Application, now: datetime) -> None:
        async with self.lock:
            today = now.date()
            yesterday = today - timedelta(days=1)
            # Совсем старые хвосты (бот был выключен) закрываем молча.
            for rec in self.store.pending_before(yesterday):
                self.store.mark_missed(rec.id)
            if self.paused():
                return
            await self.sync_day(app, today, now)
            needs_cache: dict[date, dict[str, Need]] = {}
            # Вчерашние задачи тоже: эскалация, «пропущено» и вопрос об остатках могут прийтись на после полуночи.
            for rec in self.store.for_days([yesterday, today]):
                inst = self.instance_of(rec)
                if inst is None:
                    continue
                for action in decide(self.cfg, inst, rec.state(), now):
                    if rec.day not in needs_cache:
                        needs_cache[rec.day] = self.needs(rec.day)
                    try:
                        await self.perform(app, action, rec, needs_cache[rec.day], now)
                    except Exception:
                        log.exception("Ошибка при обработке %s", rec.id)
            await self.maybe_milestones(app, now)
            await self.maybe_daily_report(app, now)

    async def perform(self, app: Application, action: Action, rec: Record, needs: dict[str, Need], now: datetime) -> None:
        inst = action.instance
        # Состояние могло измениться за этот же тик (эскалация из запасного пути, «Готово» по кнопке).
        fresh = self.store.get(inst.id)
        if fresh is None:
            return
        if action.kind in ("remind", "escalate", "miss") and fresh.status != "pending":
            return
        if action.kind == "escalate" and fresh.escalated_at is not None:
            return
        rec = fresh
        assignee = rec.assignee
        if action.kind == "remind":
            text = texts.reminder(self.cfg, inst, self.name(assignee), needs, action.repeat)
            result = await self.send(app, assignee, text, self.keyboard(inst.id), inst.id)
            if result == TRANSIENT:
                return  # повторим на следующем тике
            if result == PERMANENT and action.repeat == 0:
                # Ответственный не подключён к боту — сразу пишем запасному и родителям.
                fallback = texts.escalation(self.cfg, inst, self.name(assignee), 0, needs, unreachable=True)
                results = await self.send_many(
                    app, self.escalation_targets(inst.task, assignee), fallback, self.done_keyboard(inst.id), inst.id
                )
                if not settled(results):
                    return
                self.store.mark_escalated(inst.id, now)
            self.store.mark_sent(inst.id, now)
        elif action.kind == "escalate":
            late = int((now - inst.due).total_seconds() // 60)
            text = texts.escalation(self.cfg, inst, self.name(assignee), late, needs)
            results = await self.send_many(
                app, self.escalation_targets(inst.task, assignee), text, self.done_keyboard(inst.id), inst.id
            )
            if settled(results):
                self.store.mark_escalated(inst.id, now)
        elif action.kind == "followup":
            who = rec.done_by or assignee
            result = await self.send(app, who, texts.followup(inst, self.name(who)), self.leftovers_keyboard(inst.id))
            if result != TRANSIENT:
                self.store.mark_followup(inst.id, now)
        elif action.kind == "followup_skip":
            self.store.mark_followup(inst.id, now)
        elif action.kind == "miss":
            # Сначала гасим старые напоминания, потом шлём уведомление с кнопкой «Готово» (её не гасить).
            await self.close_messages(app, inst.id, f"❌ <b>{texts.escape(inst.task.name)}</b> ({texts.hm(inst.due)}) — не отмечено")
            results = await self.send_parents(
                app, texts.missed(inst.task, inst.due, self.name(assignee)), markup=self.done_keyboard(inst.id),
                instance_id=inst.id, silent=True,
            )
            if settled(results):
                self.store.mark_missed(inst.id)

    async def maybe_milestones(self, app: Application, now: datetime) -> None:
        if now.time() < MILESTONES_FROM:
            return
        today = now.date()
        for cat in self.cfg.cats:
            for key, when in due_milestones(cat, today):
                kv = f"milestone:{cat.id}:{key}:{when.isoformat()}"
                if self.store.get_kv(kv):
                    continue
                need = daily_need(self.cfg, cat, today, self.store.latest_weight(cat.id))
                if settled(await self.send_parents(app, texts.milestone(cat, key, need))):
                    self.store.set_kv(kv, "1")

    async def maybe_daily_report(self, app: Application, now: datetime) -> None:
        at = self.cfg.reminders.daily_report
        if at is None or now.time() < at:
            return
        key = f"report:{now.date().isoformat()}"
        if self.store.get_kv(key):
            return
        if settled(await self.send_parents(app, self.report_text(now))):
            self.store.set_kv(key, "1")

    def report_text(self, now: datetime) -> str:
        today = now.date()
        mode = day_mode(self.cfg, today, self.store.day_mode(today))
        text = texts.day_summary(self.cfg, today, mode, self.store.for_day(today), now)
        week_start = today - timedelta(days=today.weekday())
        extra = []
        for child in (m for m in self.cfg.members if not m.is_parent):
            n = self.store.done_count(child.id, week_start)
            extra.append(f"{texts.escape(child.name)} с понедельника: {n} ✅")
        missed = self.store.missed_count(week_start)
        if missed:
            extra.append(f"Пропусков за неделю: {missed}")
        if extra:
            text += "\n\n" + "\n".join(extra)
        return "🌙 <b>Итоги дня</b>\n" + text

    async def close_messages(self, app: Application, instance_id: str, text: str) -> None:
        for chat_id, message_id in self.store.messages_of(instance_id):
            try:
                await app.bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML)
            except BadRequest:
                pass  # сообщение уже изменено или удалено
            except TelegramError:
                log.warning("Не удалось обновить сообщение %s/%s", chat_id, message_id)

    # --- регистрация ---

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = self.member_of(update)
        if member_id:
            await update.effective_message.reply_text(
                f"Привет, {texts.escape(self.name(member_id))}! Я уже знаю тебя. Список команд — /help",
                parse_mode=ParseMode.HTML,
                reply_markup=MAIN_KEYBOARD,
            )
            return
        if context.args and context.args[0] == self.cfg.join_code:
            context.user_data["code_ok"] = True
            await self.ask_who(update)
            return
        context.user_data["awaiting_code"] = True
        await update.effective_message.reply_text("Привет! Я кошачий бот 🐾\nНапиши семейный код, чтобы подключиться.")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (update.effective_message.text or "").strip()
        if self.member_of(update):
            if text == BTN_HOME:
                await self.cmd_home(update, context)
            elif text == BTN_TODAY:
                await self.cmd_today(update, context)
            elif text == BTN_ALERT:
                await self.cmd_alert(update, context)
            else:
                await update.effective_message.reply_text("Не понял 🙂 Кнопки внизу или /help", reply_markup=MAIN_KEYBOARD)
            return
        if not context.user_data.get("awaiting_code"):
            await update.effective_message.reply_text("Чтобы подключиться, отправь /start")
            return
        if text == self.cfg.join_code:
            context.user_data.pop("awaiting_code", None)
            context.user_data["code_ok"] = True
            await self.ask_who(update)
        else:
            await update.effective_message.reply_text("Код не подошёл. Попробуй ещё раз или спроси у родителей.")

    async def ask_who(self, update: Update) -> None:
        buttons = [[InlineKeyboardButton(m.name, callback_data=f"reg|{m.id}")] for m in self.cfg.members]
        await update.effective_message.reply_text("Кто ты?", reply_markup=InlineKeyboardMarkup(buttons))

    async def on_register(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str) -> None:
        query = update.callback_query
        if not context.user_data.get("code_ok"):
            await query.answer("Сначала введи семейный код: /start", show_alert=True)
            return
        try:
            member = self.cfg.member(member_id)
        except KeyError:
            await query.answer("Нет такого участника", show_alert=True)
            return
        user = update.effective_user
        if member.telegram_id and (user is None or user.id != member.telegram_id):
            await query.answer("Этот участник привязан к другому аккаунту", show_alert=True)
            return
        chat_id = update.effective_chat.id
        previous_chat = self.store.chat_of(member_id)
        self.store.bind_member(member_id, chat_id, user.username if user else None, self.now())
        context.user_data.pop("code_ok", None)
        await query.answer()
        await query.edit_message_text(
            f"Готово, {texts.escape(member.name)}! Буду присылать напоминания сюда.\n\n{texts.HELP}",
            parse_mode=ParseMode.HTML,
        )
        await context.application.bot.send_message(chat_id, "Кнопки внизу всегда под рукой 👇", reply_markup=MAIN_KEYBOARD)
        app = context.application
        if previous_chat and previous_chat != chat_id:
            # Кто-то занял уже подключённого участника — пусть об этом знают все взрослые и прежний аккаунт.
            note = f"ℹ️ «{texts.escape(member.name)}» теперь подключён(а) с другого аккаунта Telegram."
            await self.send_parents(app, note + " Если это не вы — разберитесь в семье и смените join_code.", {member_id})
            try:
                await app.bot.send_message(previous_chat, note + " Этот чат отключён от бота.", parse_mode=ParseMode.HTML)
            except TelegramError:
                pass
        elif not previous_chat:
            await self.send_parents(app, f"👋 К боту подключился(-ась) {texts.escape(member.name)}.", {member_id})

    # --- кнопки напоминаний ---

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
            await query.answer("Бот работает только в личных сообщениях", show_alert=True)
            return
        action, _, payload = (query.data or "").partition("|")
        if action == "reg":
            await self.on_register(update, context, payload)
            return
        member_id = self.member_of(update)
        if not member_id:
            await query.answer("Сначала подключись: /start", show_alert=True)
            return
        if action == "mode":
            await self.on_mode(update, context, member_id, payload)
            return
        if action == "alert":
            await self.on_alert(update, context, member_id, payload)
            return
        if action == "wconf":
            await self.on_weight_confirm(update, context, member_id, payload)
            return
        if action == "left":
            value, _, payload = payload.partition("|")
        inst = self.instance(payload)
        if inst is None:
            await query.answer("Эта задача уже не актуальна", show_alert=True)
            return
        if action == "done":
            await self.on_done(update, context, member_id, inst)
        elif action == "snooze":
            await self.on_snooze(update, member_id, inst)
        elif action == "pass":
            await self.on_pass(update, context, member_id, inst)
        elif action == "left":
            await self.on_leftovers(update, context, member_id, inst, value)
        else:
            await query.answer()

    def actionable_day(self, rec: Record, now: datetime) -> bool:
        """Сегодняшние задачи и вчерашние, которые бот ещё ведёт (поздний вечер, после полуночи)."""
        return rec.day == now.date() or rec.day == now.date() - timedelta(days=1)

    async def on_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, inst: Instance) -> None:
        async with self.lock:
            await self._on_done(update, context, member_id, inst)

    async def _on_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, inst: Instance) -> None:
        query = update.callback_query
        now = self.now()
        rec = self.store.get(inst.id)
        if rec.status == "done":
            await query.answer(texts.already_done(self.name(rec.done_by), rec.done_at, inst.task.kind, rec.day, now.date()), show_alert=True)
            await self.close_messages(context.application, inst.id, texts.done_stamp(inst.task, inst.due, self.name(rec.done_by), rec.done_at))
            return
        if not self.actionable_day(rec, now):
            await query.answer(f"Это кнопка за {rec.day.strftime('%d.%m')} — уже не актуально", show_alert=True)
            return
        if rec.status == "skipped":
            await query.answer("Эту задачу отменили: сменили режим дня", show_alert=True)
            return
        if rec.due_at - now > timedelta(minutes=self.cfg.reminders.home_window_min):
            await query.answer(f"Ещё рано: {inst.task.name} в {texts.hm(rec.due_at)}", show_alert=True)
            return
        if not self.store.mark_done(inst.id, member_id, now):
            rec = self.store.get(inst.id)
            await query.answer(texts.already_done(self.name(rec.done_by), rec.done_at, inst.task.kind, rec.day, now.date()), show_alert=True)
            return
        stamp = texts.done_stamp(inst.task, inst.due, self.name(member_id), now)
        await self.close_messages(context.application, inst.id, stamp)
        if self.is_parent(member_id):
            await query.answer("Отмечено ✅")
        else:
            n = self.store.done_count(member_id, now.date() - timedelta(days=now.weekday()))
            await query.answer(texts.PRAISE[n % len(texts.PRAISE)], show_alert=True)
            if self.cfg.reminders.notify_parents_on_child_done:
                await self.send_parents(context.application, stamp)

    async def on_leftovers(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, inst: Instance, value: str) -> None:
        query = update.callback_query
        if value not in ("ok", "many"):
            await query.answer()
            return
        rec = self.store.get(inst.id)
        if rec.leftovers is not None:
            await query.answer("Уже записано 👍")
            return
        self.store.set_leftovers(inst.id, value)
        if value == "ok":
            await query.answer("Отлично! 👍")
            await self._edit_query(query, f"👍 {texts.escape(inst.task.name)}: всё съели, остатки убраны.")
            return
        await query.answer("Записал. Родителям сообщу.")
        await self._edit_query(query, f"🍽 {texts.escape(inst.task.name)}: осталось много — остатки выброшены, родителям сообщил.")
        followup_tasks = {t.id for t in self.cfg.tasks if t.followup}
        prev = self.store.previous_followup(inst.id, followup_tasks)
        twice = prev is not None and prev.leftovers == "many"
        await self.send_parents(
            context.application, texts.leftovers_alert(inst, self.name(member_id), twice), exclude={member_id}
        )

    async def _edit_query(self, query, text: str) -> None:
        try:
            await query.edit_message_text(text, parse_mode=ParseMode.HTML)
        except TelegramError:
            pass

    async def _check_own_pending(self, query, member_id: str, inst: Instance) -> Record | None:
        """Отложить или передать можно только свою, ещё не сделанную сегодняшнюю задачу."""
        rec = self.store.get(inst.id)
        now = self.now()
        if rec.status == "done":
            await query.answer(texts.already_done(self.name(rec.done_by), rec.done_at, inst.task.kind, rec.day, now.date()), show_alert=True)
            return None
        if rec.status != "pending" or not self.actionable_day(rec, now):
            await query.answer("Эта задача уже не актуальна", show_alert=True)
            return None
        if rec.assignee != member_id:
            await query.answer(
                f"Это напоминание для: {self.name(rec.assignee)}. Если сделал(а) сам(а) — жми «Готово».", show_alert=True
            )
            return None
        return rec

    async def on_snooze(self, update: Update, member_id: str, inst: Instance) -> None:
        query = update.callback_query
        async with self.lock:
            if not await self._check_own_pending(query, member_id, inst):
                return
            minutes = self.cfg.reminders.snooze_min
            self.store.snooze(inst.id, self.now() + timedelta(minutes=minutes))
        await query.answer(f"Хорошо, напомню через {minutes} мин ⏰")

    async def on_pass(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, inst: Instance) -> None:
        query = update.callback_query
        async with self.lock:
            if not await self._check_own_pending(query, member_id, inst):
                return
            target = next_assignee(self.cfg, inst.task, member_id)
            while target and self.store.chat_of(target) is None:
                target = next_assignee(self.cfg, inst.task, target)
            if not target:
                await query.answer("Больше некому передать — позвони родителям 📞", show_alert=True)
                return
            self.store.reassign(inst.id, target)
            await query.answer(f"Передал: {self.name(target)}")
            text = texts.handed_off(self.cfg, inst, self.name(member_id), self.name(target), self.needs(inst.day))
            if await self.send(context.application, target, text, self.keyboard(inst.id), inst.id) == OK:
                self.store.mark_sent(inst.id, self.now())
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass

    # --- команды ---

    async def guard(self, update: Update) -> str | None:
        if update.effective_chat is None or update.effective_chat.type != ChatType.PRIVATE:
            return None
        member_id = self.member_of(update)
        if not member_id:
            await update.effective_message.reply_text("Сначала подключись: /start")
        return member_id

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        markup = MAIN_KEYBOARD if self.member_of(update) else None
        await update.effective_message.reply_text(texts.HELP, parse_mode=ParseMode.HTML, reply_markup=markup)

    async def cmd_home(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = await self.guard(update)
        if not member_id:
            return
        now = self.now()
        app = context.application
        if self.cfg.reminders.notify_parents_on_home and not self.is_parent(member_id):
            await self.send_parents(app, f"🏠 {texts.escape(self.name(member_id))} дома ({texts.hm(now)})", exclude={member_id})
        if self.paused():
            await update.effective_message.reply_text("Привет! 🏠 Напоминания сейчас на паузе.")
            return
        pulled = 0
        async with self.lock:
            await self.sync_day(app, now.date(), now)
            needs = self.needs(now.date())
            for rec in self.store.for_day(now.date()):
                inst = self.instance_of(rec)
                if inst is None or not home_pull(self.cfg, inst, rec.state(), member_id, now):
                    continue
                text = texts.reminder(self.cfg, inst, self.name(member_id), needs, 0)
                if await self.send(app, member_id, text, self.keyboard(inst.id), inst.id) == OK:
                    self.store.mark_sent(inst.id, now)
                    pulled += 1
        if pulled:
            return
        records = [(r, self.task_of(r)) for r in self.store.for_day(now.date())]
        records = [(r, t) for r, t in records if t is not None and r.status == "pending"]
        waiting = [t.name for r, t in records if r.assignee == member_id and r.sent_count > 0]
        if waiting:
            names = ", ".join(texts.escape(n) for n in waiting)
            await update.effective_message.reply_text(f"Привет! 🏠 Тебя ждёт: {names}. Напоминание выше ⬆️", parse_mode=ParseMode.HTML)
            return
        upcoming = [(r, t) for r, t in records if r.due_at > now]
        if upcoming:
            r, t = upcoming[0]
            await update.effective_message.reply_text(
                f"Привет! 🏠 Пока ничего не нужно. Следующее: {texts.hm(r.due_at)} "
                f"{texts.escape(t.name)} ({texts.escape(self.name(r.assignee))}).",
                parse_mode=ParseMode.HTML,
            )
        else:
            await update.effective_message.reply_text("Привет! 🏠 На сегодня всё сделано или больше ничего нет.")

    async def cmd_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        rows = [[InlineKeyboardButton(f"{c.emoji} {c.name}", callback_data=f"alert|{c.id}")] for c in self.cfg.cats]
        rows.append([InlineKeyboardButton("Обе", callback_data="alert|*")])
        await update.effective_message.reply_text("С кем что-то не так?", reply_markup=InlineKeyboardMarkup(rows))

    async def on_alert(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, payload: str) -> None:
        query = update.callback_query
        cat_id, _, symptom = payload.partition("|")
        if cat_id != "*" and cat_id not in {c.id for c in self.cfg.cats}:
            await query.answer()
            return
        if not symptom:
            rows = [
                [InlineKeyboardButton(label, callback_data=f"alert|{cat_id}|{key}")]
                for key, label in texts.SYMPTOMS.items()
            ]
            await query.answer()
            await query.edit_message_text("Что случилось?", reply_markup=InlineKeyboardMarkup(rows))
            return
        if symptom not in texts.SYMPTOMS:
            await query.answer()
            return
        cat_name = "Обе кошки" if cat_id == "*" else self.cfg.cat(cat_id).name
        text = texts.alert(self.name(member_id), cat_name, texts.SYMPTOMS[symptom], self.now())
        results = await self.send_parents(context.application, text, exclude={member_id})
        await query.answer()
        if OK in results:
            reply = "Родителям сообщил 📨 Если кошке совсем плохо — сразу позвони маме или папе."
        else:
            reply = "Не получилось никому отправить. Позвони родителям или ветеринару, если кошке плохо 📞"
        await self._edit_query(query, reply)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = await self.guard(update)
        if not member_id:
            return
        now = self.now()
        async with self.lock:
            mode, _ = await self.sync_day(context.application, now.date(), now)
        records = self.store.for_day(now.date())
        text = texts.day_summary(self.cfg, now.date(), mode, records, now)
        if self.paused():
            text = "⏸ Напоминания на паузе (/resume — включить)\n\n" + text
        window = timedelta(minutes=self.cfg.reminders.home_window_min)
        buttons = []
        for r in records:
            task = self.task_of(r)
            if task is None or r.status not in ("pending", "missed") or r.due_at - now > window:
                continue
            if not self.is_parent(member_id) and r.assignee != member_id:
                continue  # ребёнок отмечает только свои задачи
            buttons.append([InlineKeyboardButton(f"✅ {texts.hm(r.due_at)} {task.name}", callback_data=f"done|{r.id}")])
        await update.effective_message.reply_text(
            text, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
        )

    async def cmd_ration(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        today = self.now().date()
        weighed = {c.id: (self.store.weights(c.id, 1) or [(None, None)])[0][0] for c in self.cfg.cats}
        text = texts.ration_text(self.cfg, self.needs(today), weighed, plans_by_mode(self.cfg, today))
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML)

    async def cmd_weight(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = await self.guard(update)
        if not member_id:
            return
        usage = "Напиши так: <code>/weight Младшая 1.6</code> (вес в килограммах)"
        args = context.args or []
        if len(args) < 2:
            await update.effective_message.reply_text(usage, parse_mode=ParseMode.HTML)
            return
        cat_query = " ".join(args[:-1]).lower()
        matches = [c for c in self.cfg.cats if c.name.lower() == cat_query or c.id == cat_query]
        if not matches:
            matches = [c for c in self.cfg.cats if c.name.lower().startswith(cat_query)]
        try:
            kg = float(args[-1].replace(",", "."))
        except ValueError:
            kg = 0
        if kg > 50:  # ввели граммы
            kg = kg / 1000
        if len(matches) != 1 or not 0.2 <= kg <= 15:
            await update.effective_message.reply_text(usage, parse_mode=ParseMode.HTML)
            return
        cat = matches[0]
        previous = self.store.latest_weight(cat.id) or cat.weight_kg
        if abs(kg / previous - 1) > WEIGHT_JUMP:
            button = InlineKeyboardButton(
                f"Да, {texts.num(kg)} кг", callback_data=f"wconf|{cat.id}|{round(kg * 1000)}"
            )
            await update.effective_message.reply_text(
                f"Точно {texts.num(kg)} кг? Было {texts.num(previous)} кг — это слишком резкое изменение. "
                "Если опечатка — отправь /weight ещё раз.",
                reply_markup=InlineKeyboardMarkup([[button]]),
            )
            return
        await self.save_weight(update.effective_message.reply_text, context.application, member_id, cat.id, kg)

    async def on_weight_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, payload: str) -> None:
        query = update.callback_query
        cat_id, _, grams = payload.partition("|")
        if cat_id not in {c.id for c in self.cfg.cats} or not grams.isdigit() or not 200 <= int(grams) <= 15000:
            await query.answer()
            return
        await query.answer()

        async def reply(text, parse_mode=None):
            await self._edit_query(query, text)

        await self.save_weight(reply, context.application, member_id, cat_id, int(grams) / 1000)

    async def save_weight(self, reply, app: Application, member_id: str, cat_id: str, kg: float) -> None:
        cat = self.cfg.cat(cat_id)
        today = self.now().date()
        self.store.add_weight(cat.id, today, kg, member_id)
        need = daily_need(self.cfg, cat, today, kg)
        text = texts.weight_saved(cat.name, kg, self.store.weights(cat.id, 2), need)
        await reply(text, parse_mode=ParseMode.HTML)
        if not self.is_parent(member_id):
            await self.send_parents(app, f"⚖️ {texts.escape(self.name(member_id))} записал(а) вес.\n" + text)

    async def cmd_plan(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        await update.effective_message.reply_text(self.plan_text or "Правила не заданы.", parse_mode=ParseMode.HTML)

    async def cmd_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = await self.guard(update)
        if not member_id:
            return
        if not self.is_parent(member_id):
            await update.effective_message.reply_text("Режим дня меняют родители 🙂")
            return
        today = self.now().date()
        rows = []
        for label, day in (("Сегодня", today), ("Завтра", today + timedelta(days=1))):
            row = [InlineKeyboardButton(f"{label}: {self.cfg.mode_titles[m]}", callback_data=f"mode|{day.isoformat()}:{m}") for m in self.cfg.modes]
            row.append(InlineKeyboardButton(f"{label}: авто", callback_data=f"mode|{day.isoformat()}:auto"))
            rows += [row[i : i + 2] for i in range(0, len(row), 2)]
        cur_today = day_mode(self.cfg, today, self.store.day_mode(today))
        cur_tomorrow = day_mode(self.cfg, today + timedelta(days=1), self.store.day_mode(today + timedelta(days=1)))
        await update.effective_message.reply_text(
            f"Сейчас: сегодня — {self.cfg.mode_titles[cur_today]}, завтра — {self.cfg.mode_titles[cur_tomorrow]}.\nВыбери режим:",
            reply_markup=InlineKeyboardMarkup(rows),
        )

    async def on_mode(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, payload: str) -> None:
        query = update.callback_query
        if not self.is_parent(member_id):
            await query.answer("Режим дня меняют родители", show_alert=True)
            return
        day_str, _, mode = payload.partition(":")
        try:
            day = date.fromisoformat(day_str)
        except ValueError:
            await query.answer()
            return
        now = self.now()
        if day < now.date() or day > now.date() + timedelta(days=1) or (mode != "auto" and mode not in self.cfg.modes):
            await query.answer("Эта кнопка уже не актуальна", show_alert=True)
            return
        async with self.lock:
            self.store.set_day_mode(day, None if mode == "auto" else mode)
            mode_now, _ = await self.sync_day(context.application, day, now)
        await query.answer("Готово")
        text = texts.day_summary(self.cfg, day, mode_now, self.store.for_day(day), now)
        await query.edit_message_text("Режим изменён.\n" + text, parse_mode=ParseMode.HTML)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = await self.guard(update)
        if not member_id:
            return
        if not self.is_parent(member_id):
            await update.effective_message.reply_text("Паузу ставят родители 🙂")
            return
        self.store.set_kv("paused", "1")
        await update.effective_message.reply_text("⏸ Напоминания на паузе. Включить снова — /resume")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        member_id = await self.guard(update)
        if not member_id:
            return
        if not self.is_parent(member_id):
            await update.effective_message.reply_text("Паузу снимают родители 🙂")
            return
        now = self.now()
        async with self.lock:
            self.store.set_kv("paused", None)
            await self.sync_day(context.application, now.date(), now)
            # Не засыпаем пропущенными за время паузы напоминаниями.
            stale = now - timedelta(minutes=self.cfg.reminders.escalate_after_min)
            for rec in self.store.for_days([now.date() - timedelta(days=1), now.date()]):
                if rec.status == "pending" and rec.due_at < stale:
                    self.store.mark_missed(rec.id)
        await update.effective_message.reply_text("▶️ Напоминания снова включены. План на сегодня — /today")


def plans_by_mode(cfg: Config, day: date) -> dict[str, list[Instance]]:
    """Кормления в каждом режиме дня (для /ration). День недели берём сегодняшний."""
    result = {}
    for mode in cfg.modes:
        insts = [i for i in instances_for_day(cfg, day, mode) if i.task.portions]
        if insts:
            result[mode] = insts
    return result


def build_application(token: str, cfg: Config, store: Store, plan_text: str = "") -> Application:
    bot = CatBot(cfg, store, plan_text)

    async def post_init(app: Application) -> None:
        await app.bot.set_my_commands(COMMANDS)

    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.bot_data["catbot"] = bot
    private = filters.ChatType.PRIVATE
    for name, handler in (
        ("start", bot.cmd_start),
        ("help", bot.cmd_help),
        ("today", bot.cmd_today),
        ("home", bot.cmd_home),
        ("alert", bot.cmd_alert),
        ("ration", bot.cmd_ration),
        ("weight", bot.cmd_weight),
        ("plan", bot.cmd_plan),
        ("mode", bot.cmd_mode),
        ("pause", bot.cmd_pause),
        ("resume", bot.cmd_resume),
    ):
        app.add_handler(CommandHandler(name, handler, filters=private))
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & private, bot.on_text))
    app.job_queue.run_repeating(bot.tick, interval=cfg.tick_seconds, first=3)
    return app
