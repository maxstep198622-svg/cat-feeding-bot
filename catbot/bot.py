"""Telegram-часть: команды, кнопки и периодический «тик», который рассылает напоминания."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
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
from .config import Config
from .ration import Need, daily_need, due_milestones
from .scheduler import Action, Instance, day_mode, decide, instances_for_day, next_assignee
from .storage import Store

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand("today", "План на сегодня"),
    BotCommand("ration", "Порции и расчёт нормы"),
    BotCommand("weight", "Записать вес кошки"),
    BotCommand("plan", "Правила кормления"),
    BotCommand("mode", "Режим дня (родители)"),
    BotCommand("pause", "Пауза напоминаний (родители)"),
    BotCommand("resume", "Снять паузу (родители)"),
    BotCommand("help", "Справка"),
]


class CatBot:
    def __init__(self, cfg: Config, store: Store, plan_text: str = ""):
        self.cfg = cfg
        self.store = store
        self.plan_text = plan_text

    # --- вспомогательное ---

    def now(self) -> datetime:
        return datetime.now(self.cfg.timezone)

    def name(self, member_id: str | None) -> str:
        return texts._name(self.cfg, member_id)

    def needs(self, today: date) -> dict[str, Need]:
        return {c.id: daily_need(self.cfg, c, today, self.store.latest_weight(c.id)) for c in self.cfg.cats}

    def today_plan(self, day: date) -> tuple[str, list[Instance]]:
        override = self.store.day_mode(day)
        mode = day_mode(self.cfg, day, override)
        insts = instances_for_day(self.cfg, day, override)
        self.store.ensure_instances(insts)
        self.store.sync_due(insts)
        self.store.drop_unplanned(day, {i.id for i in insts})
        return mode, insts

    def instance(self, instance_id: str) -> Instance | None:
        rec = self.store.get(instance_id)
        if rec is None:
            return None
        try:
            task = self.cfg.task(rec.task_id)
        except KeyError:
            return None
        return Instance(rec.id, task, rec.day, rec.due_at)

    def paused(self) -> bool:
        return self.store.get_kv("paused") == "1"

    def keyboard(self, instance_id: str) -> InlineKeyboardMarkup:
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

    def member_of(self, update: Update) -> str | None:
        chat = update.effective_chat
        return self.store.member_by_chat(chat.id) if chat else None

    def is_parent(self, member_id: str | None) -> bool:
        if not member_id:
            return False
        try:
            return self.cfg.member(member_id).is_parent
        except KeyError:
            return False

    async def send(self, app: Application, member_id: str, text: str, markup=None, instance_id: str | None = None) -> bool:
        chat_id = self.store.chat_of(member_id)
        if chat_id is None:
            return False
        try:
            msg = await app.bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Forbidden:
            log.warning("Участник %s заблокировал бота", member_id)
            return False
        except TelegramError:
            log.exception("Не удалось отправить сообщение %s", member_id)
            return False
        if instance_id:
            self.store.add_message(instance_id, chat_id, msg.message_id)
        return True

    async def send_parents(self, app: Application, text: str, exclude: set[str] = frozenset(), markup=None, instance_id=None) -> int:
        sent = 0
        for m in self.cfg.parents:
            if m.id not in exclude and await self.send(app, m.id, text, markup, instance_id):
                sent += 1
        return sent

    # --- тик ---

    async def tick(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            await self.run_tick(context.application, self.now())
        except Exception:
            log.exception("Ошибка в тике")

    async def run_tick(self, app: Application, now: datetime) -> None:
        today = now.date()
        for rec in self.store.pending_before(today):
            self.store.mark_missed(rec.id)
        if self.paused():
            return
        _, insts = self.today_plan(today)
        needs = self.needs(today)
        for inst in insts:
            rec = self.store.get(inst.id)
            if rec is None:
                continue
            inst = Instance(inst.id, inst.task, inst.day, rec.due_at)
            for action in decide(self.cfg, inst, rec.state(), now):
                await self.perform(app, action, rec.assignee, needs, now)
        await self.maybe_milestones(app, now)
        await self.maybe_daily_report(app, now)

    async def perform(self, app: Application, action: Action, assignee: str, needs: dict[str, Need], now: datetime) -> None:
        inst = action.instance
        if action.kind == "remind":
            text = texts.reminder(self.cfg, inst, self.name(assignee), needs, action.repeat)
            ok = await self.send(app, assignee, text, self.keyboard(inst.id), inst.id)
            if not ok and action.repeat == 0:
                # Ответственный не зарегистрирован в боте — сразу пишем родителям.
                fallback = texts.escalation(self.cfg, inst, self.name(assignee), 0, needs)
                await self.send_parents(app, fallback, {assignee}, self.keyboard(inst.id), inst.id)
            self.store.mark_sent(inst.id, now)
        elif action.kind == "escalate":
            late = int((now - inst.due).total_seconds() // 60)
            text = texts.escalation(self.cfg, inst, self.name(assignee), late, needs)
            targets = []
            if inst.task.backup and inst.task.backup != assignee:
                targets.append(inst.task.backup)
            targets += [m.id for m in self.cfg.parents if m.id not in targets and m.id != assignee]
            for member_id in targets:
                await self.send(app, member_id, text, self.keyboard(inst.id), inst.id)
            self.store.mark_escalated(inst.id, now)
        elif action.kind == "miss":
            if self.store.mark_missed(inst.id):
                await self.close_messages(app, inst.id, f"❌ <b>{texts.escape(inst.task.name)}</b> ({texts.hm(inst.due)}) — не отмечено")
                await self.send_parents(app, texts.missed(inst.task, inst.due, self.name(assignee)))

    async def maybe_milestones(self, app: Application, now: datetime) -> None:
        today = now.date()
        for cat in self.cfg.cats:
            for key, when in due_milestones(cat, today):
                kv = f"milestone:{cat.id}:{key}:{when.isoformat()}"
                if self.store.get_kv(kv):
                    continue
                self.store.set_kv(kv, "1")
                need = daily_need(self.cfg, cat, today, self.store.latest_weight(cat.id))
                await self.send_parents(app, texts.milestone(cat, key, need))

    async def maybe_daily_report(self, app: Application, now: datetime) -> None:
        at = self.cfg.reminders.daily_report
        if at is None or now.time() < at:
            return
        key = f"report:{now.date().isoformat()}"
        if self.store.get_kv(key):
            return
        self.store.set_kv(key, "1")
        await self.send_parents(app, self.report_text(now))

    def report_text(self, now: datetime) -> str:
        today = now.date()
        mode, _ = self.today_plan(today)
        text = texts.day_summary(self.cfg, today, mode, self.store.for_day(today), now)
        week_start = today - timedelta(days=today.weekday())
        children = [m for m in self.cfg.members if not m.is_parent]
        extra = []
        for child in children:
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
                f"Привет, {self.name(member_id)}! Я уже знаю тебя. Список команд — /help", parse_mode=ParseMode.HTML
            )
            return
        if context.args and context.args[0] == self.cfg.join_code:
            await self.ask_who(update)
            return
        context.user_data["awaiting_code"] = True
        await update.effective_message.reply_text("Привет! Я кошачий бот 🐾\nНапиши семейный код, чтобы подключиться.")

    async def on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not context.user_data.get("awaiting_code"):
            if not self.member_of(update):
                await update.effective_message.reply_text("Чтобы подключиться, отправь /start")
            return
        if update.effective_message.text.strip() == self.cfg.join_code:
            context.user_data.pop("awaiting_code", None)
            await self.ask_who(update)
        else:
            await update.effective_message.reply_text("Код не подошёл. Попробуй ещё раз или спроси у родителей.")

    async def ask_who(self, update: Update) -> None:
        buttons = [[InlineKeyboardButton(m.name, callback_data=f"reg|{m.id}")] for m in self.cfg.members]
        await update.effective_message.reply_text("Кто ты?", reply_markup=InlineKeyboardMarkup(buttons))

    async def on_register(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str) -> None:
        query = update.callback_query
        try:
            member = self.cfg.member(member_id)
        except KeyError:
            await query.answer("Нет такого участника", show_alert=True)
            return
        user = update.effective_user
        if member.telegram_id and user and user.id != member.telegram_id:
            await query.answer("Этот участник привязан к другому аккаунту", show_alert=True)
            return
        previous_chat = self.store.chat_of(member_id)
        self.store.bind_member(member_id, update.effective_chat.id, user.username if user else None, self.now())
        await query.answer()
        await query.edit_message_text(
            f"Готово, {texts.escape(member.name)}! Буду присылать напоминания сюда.\n\n{texts.HELP}",
            parse_mode=ParseMode.HTML,
        )
        if previous_chat and previous_chat != update.effective_chat.id:
            await self.send_parents(
                context.application, f"ℹ️ {texts.escape(member.name)} подключился(-ась) к боту с другого аккаунта.", {member_id}
            )

    # --- кнопки напоминаний ---

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
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
        else:
            await query.answer()

    async def on_done(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, inst: Instance) -> None:
        query = update.callback_query
        now = self.now()
        if not self.store.mark_done(inst.id, member_id, now):
            rec = self.store.get(inst.id)
            await query.answer(texts.already_done(self.name(rec.done_by), rec.done_at, inst.task.kind), show_alert=True)
            await self.close_messages(context.application, inst.id, texts.done_stamp(inst.task, inst.due, self.name(rec.done_by), rec.done_at))
            return
        stamp = texts.done_stamp(inst.task, inst.due, self.name(member_id), now)
        await self.close_messages(context.application, inst.id, stamp)
        member = self.cfg.member(member_id)
        if member.is_parent:
            await query.answer("Отмечено ✅")
        else:
            n = self.store.done_count(member_id, now.date() - timedelta(days=now.weekday()))
            await query.answer(texts.PRAISE[n % len(texts.PRAISE)], show_alert=True)
            if self.cfg.reminders.notify_parents_on_child_done:
                await self.send_parents(context.application, stamp)

    async def on_snooze(self, update: Update, member_id: str, inst: Instance) -> None:
        minutes = self.cfg.reminders.snooze_min
        self.store.snooze(inst.id, self.now() + timedelta(minutes=minutes))
        await update.callback_query.answer(f"Хорошо, напомню через {minutes} мин ⏰")

    async def on_pass(self, update: Update, context: ContextTypes.DEFAULT_TYPE, member_id: str, inst: Instance) -> None:
        query = update.callback_query
        rec = self.store.get(inst.id)
        if rec.status == "done":
            await query.answer(texts.already_done(self.name(rec.done_by), rec.done_at, inst.task.kind), show_alert=True)
            return
        target = next_assignee(self.cfg, inst.task, member_id)
        while target and self.store.chat_of(target) is None:
            target = next_assignee(self.cfg, inst.task, target)
        if not target:
            await query.answer("Больше некому передать — попробуй позвонить родителям 📞", show_alert=True)
            return
        self.store.reassign(inst.id, target)
        await query.answer(f"Передал: {self.name(target)}")
        needs = self.needs(inst.day)
        text = texts.handed_off(self.cfg, inst, self.name(member_id), self.name(target), needs)
        if await self.send(context.application, target, text, self.keyboard(inst.id), inst.id):
            self.store.mark_sent(inst.id, self.now())
        try:
            await query.edit_message_reply_markup(None)
        except TelegramError:
            pass

    # --- команды ---

    async def guard(self, update: Update) -> str | None:
        member_id = self.member_of(update)
        if not member_id:
            await update.effective_message.reply_text("Сначала подключись: /start")
        return member_id

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(texts.HELP, parse_mode=ParseMode.HTML)

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self.guard(update):
            return
        now = self.now()
        mode, _ = self.today_plan(now.date())
        records = self.store.for_day(now.date())
        text = texts.day_summary(self.cfg, now.date(), mode, records, now)
        if self.paused():
            text = "⏸ Напоминания на паузе (/resume — включить)\n\n" + text
        buttons = []
        for r in records:
            if r.status != "done":
                task = self.cfg.task(r.task_id)
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
        cat = next((c for c in self.cfg.cats if c.name.lower().startswith(cat_query) or c.id == cat_query), None)
        try:
            kg = float(args[-1].replace(",", "."))
        except ValueError:
            kg = 0
        if kg > 50:  # ввели граммы
            kg = kg / 1000
        if cat is None or not 0.2 <= kg <= 15:
            await update.effective_message.reply_text(usage, parse_mode=ParseMode.HTML)
            return
        today = self.now().date()
        self.store.add_weight(cat.id, today, kg, member_id)
        need = daily_need(self.cfg, cat, today, kg)
        await update.effective_message.reply_text(
            texts.weight_saved(cat.name, kg, self.store.weights(cat.id, 2), need), parse_mode=ParseMode.HTML
        )

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
        day = date.fromisoformat(day_str)
        if day < self.now().date():
            await query.answer("Этот день уже прошёл", show_alert=True)
            return
        self.store.set_day_mode(day, None if mode == "auto" else mode)
        mode_now, insts = self.today_plan(day)
        await query.answer("Готово")
        lines = [f"Режим на {day.strftime('%d.%m')}: <b>{texts.escape(self.cfg.mode_titles[mode_now])}</b>"]
        lines += [f"• {texts.hm(i.due)} {texts.escape(i.task.name)} — {texts.escape(self.name(i.task.primary))}" for i in insts]
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML)

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
        self.store.set_kv("paused", None)
        # Не засыпаем пропущенными за время паузы напоминаниями.
        now = self.now()
        _, insts = self.today_plan(now.date())
        for inst in insts:
            rec = self.store.get(inst.id)
            if rec and rec.status == "pending" and rec.due_at < now - timedelta(minutes=self.cfg.reminders.escalate_after_min):
                self.store.mark_missed(inst.id)
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
    app.add_handler(CommandHandler("start", bot.cmd_start))
    app.add_handler(CommandHandler("help", bot.cmd_help))
    app.add_handler(CommandHandler("today", bot.cmd_today))
    app.add_handler(CommandHandler("ration", bot.cmd_ration))
    app.add_handler(CommandHandler("weight", bot.cmd_weight))
    app.add_handler(CommandHandler("plan", bot.cmd_plan))
    app.add_handler(CommandHandler("mode", bot.cmd_mode))
    app.add_handler(CommandHandler("pause", bot.cmd_pause))
    app.add_handler(CommandHandler("resume", bot.cmd_resume))
    app.add_handler(CallbackQueryHandler(bot.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, bot.on_text))
    app.job_queue.run_repeating(bot.tick, interval=cfg.tick_seconds, first=3)
    return app
