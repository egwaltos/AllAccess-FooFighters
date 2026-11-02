"""
Interactive Telegram Bot Command & Callback Listener for AllAccess Monitor (@allaccess_emi_bot).

Provides real-time (<0.5s) long-polling command & inline button handling:
  - /start, /ayuda : Main interactive control panel with inline buttons
  - /estado        : Live daemon metrics, auto-reserve status, and watched targets
  - /chequear      : Immediate on-demand live check across all watched targets
  - /sectores      : Inspect all sectors & prices of the primary AllAccess event
  - /agregar       : Add a new sector or event URL + sector to live monitoring
  - /quitar        : Remove a watched target (with 1-tap inline buttons)
  - /test          : Trigger a simulated availability & cart hold alert
"""
from datetime import datetime, timezone
import html
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from allaccess_monitor.checker.allaccess import DEFAULT_BROWSER_HEADERS
from allaccess_monitor.models import AvailabilityStatus, MonitoredTarget

logger = logging.getLogger("allaccess_monitor.telegram_bot")


class TelegramCommandBot:
    """Real-time Telegram Bot listener and interactive panel for AllAccess Monitor."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.bot_token: str = (service.config.telegram.bot_token or "").strip()
        self.allowed_chat_id: str = str(service.config.telegram.chat_id or "").strip()
        self.api_base = f"https://api.telegram.org/bot{self.bot_token}"
        self._session = requests.Session()
        self._offset: int = 0
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    @property
    def is_configured(self) -> bool:
        return bool(self.bot_token and self.allowed_chat_id)

    def start(self) -> None:
        """Start background long-polling thread for Telegram commands & inline buttons."""
        if not self.is_configured:
            logger.info("TelegramCommandBot not started (token or chat_id missing).")
            return

        self._register_bot_commands()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="telegram-bot-listener",
            daemon=True,
        )
        self._thread.start()
        logger.info("Telegram interactive bot listener started for chat_id=%s", self.allowed_chat_id)

    def stop(self) -> None:
        self._stop_event.set()

    def _register_bot_commands(self) -> None:
        """Register slash command menu in Telegram UI via setMyCommands."""
        commands = [
            {"command": "start", "description": "🎸 Panel interactivo principal"},
            {"command": "chequear", "description": "🔍 Chequear entradas en vivo ahora"},
            {"command": "reservar", "description": "🛒 Probar reserva REAL en AllAccess"},
            {"command": "sectores", "description": "🏟️ Ver todos los sectores y precios del show"},
            {"command": "agregar", "description": "➕ Agregar sector o evento a vigilar"},
            {"command": "quitar", "description": "➖ Quitar un sector vigilado"},
            {"command": "token", "description": "🔑 Actualizar token crowder de sesión"},
            {"command": "estado", "description": "📊 Estado del monitor y auto-reserva"},
            {"command": "test", "description": "🧪 Probar alerta simulada"},
        ]
        try:
            self._session.post(
                f"{self.api_base}/setMyCommands",
                json={"commands": commands},
                timeout=10,
            )
        except Exception as exc:
            logger.debug("Could not register Telegram commands: %s", exc)

    def _main_keyboard(self) -> Dict[str, Any]:
        return {
            "inline_keyboard": [
                [
                    {"text": "🔍 Chequear Ahora", "callback_data": "cmd:check"},
                    {"text": "📊 Ver Estado", "callback_data": "cmd:status"},
                ],
                [
                    {"text": "🏟️ Sectores del Show", "callback_data": "cmd:sectors"},
                    {"text": "➕/➖ Gestionar Sectores", "callback_data": "cmd:manage"},
                ],
                [
                    {"text": "🛒 Probar Reserva REAL (Campo Delantero)", "callback_data": "cmd:live_reserve"},
                ],
                [
                    {"text": "🧪 Simulacro (Mock)", "callback_data": "cmd:test_reserve"},
                ],
            ]
        }

    def _send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            self._session.post(f"{self.api_base}/sendMessage", json=payload, timeout=10)
        except Exception as exc:
            logger.warning("Failed to send Telegram bot reply: %s", exc)

    def _answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            self._session.post(
                f"{self.api_base}/answerCallbackQuery",
                json={"callback_query_id": callback_id, "text": text},
                timeout=5,
            )
        except Exception:
            pass

    def _poll_loop(self) -> None:
        # Flush stale updates on startup so old buffered commands don't re-fire
        try:
            resp = self._session.get(
                f"{self.api_base}/getUpdates",
                params={"offset": -1, "timeout": 1},
                timeout=5,
            )
            if resp.ok:
                items = resp.json().get("result") or []
                if items:
                    self._offset = int(items[-1]["update_id"]) + 1
        except Exception:
            pass

        while not self._stop_event.is_set():
            try:
                resp = self._session.get(
                    f"{self.api_base}/getUpdates",
                    params={
                        "offset": self._offset,
                        "timeout": 20,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=25,
                )
                if not resp.ok:
                    self._stop_event.wait(5.0)
                    continue

                updates = resp.json().get("result") or []
                for upd in updates:
                    self._offset = max(self._offset, int(upd["update_id"]) + 1)
                    try:
                        self._handle_update(upd)
                    except Exception as exc:
                        logger.exception("Error handling Telegram update: %s", exc)
            except Exception as exc:
                logger.debug("Telegram poll loop transient error: %s", exc)
                self._stop_event.wait(3.0)

    def _handle_update(self, upd: Dict[str, Any]) -> None:
        if "callback_query" in upd:
            cb = upd["callback_query"]
            cb_id = str(cb.get("id", ""))
            msg = cb.get("message") or {}
            chat_id = str((msg.get("chat") or {}).get("id", ""))
            if chat_id != self.allowed_chat_id:
                self._answer_callback(cb_id, "No autorizado.")
                return
            data = str(cb.get("data", ""))
            self._handle_callback(chat_id, cb_id, data)
            return

        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if not chat_id or chat_id != self.allowed_chat_id:
            return

        text = str(msg.get("text") or "").strip()
        if not text:
            return

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower().split("@")[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd in ("/start", "/ayuda", "/help"):
            self._cmd_start(chat_id)
        elif cmd in ("/estado", "/status"):
            self._cmd_status(chat_id)
        elif cmd in ("/chequear", "/check"):
            self._cmd_check(chat_id)
        elif cmd in ("/sectores", "/sectors"):
            self._cmd_sectors(chat_id)
        elif cmd in ("/agregar", "/add"):
            self._cmd_add(chat_id, arg)
        elif cmd in ("/quitar", "/remove", "/del"):
            self._cmd_remove(chat_id, arg)
        elif cmd in ("/reservar", "/reserve"):
            self._cmd_live_reserve(chat_id, arg)
        elif cmd in ("/token",):
            self._cmd_token(chat_id, arg)
        elif cmd in ("/test",):
            self._cmd_test_reserve(chat_id)
        else:
            self._cmd_start(chat_id)

    def _handle_callback(self, chat_id: str, cb_id: str, data: str) -> None:
        if data == "cmd:check":
            self._answer_callback(cb_id, "Consultando AllAccess en vivo...")
            self._cmd_check(chat_id)
        elif data == "cmd:status":
            self._answer_callback(cb_id, "Cargando estado...")
            self._cmd_status(chat_id)
        elif data == "cmd:sectors":
            self._answer_callback(cb_id, "Inspeccionando sectores en AllAccess...")
            self._cmd_sectors(chat_id)
        elif data == "cmd:manage":
            self._answer_callback(cb_id, "Gestión de sectores")
            self._cmd_manage_menu(chat_id)
        elif data == "cmd:live_reserve":
            self._answer_callback(cb_id, "Intentando reserva real en AllAccess...")
            self._cmd_live_reserve(chat_id, "Campo Delantero")
        elif data == "cmd:test_reserve":
            self._answer_callback(cb_id, "Ejecutando simulacro de reserva...")
            self._cmd_test_reserve(chat_id)
        elif data.startswith("add_sec:"):
            sec_name = data.split(":", 1)[1]
            self._answer_callback(cb_id, f"Agregando {sec_name}...")
            self._cmd_add(chat_id, sec_name)
        elif data.startswith("rem_idx:"):
            idx_str = data.split(":", 1)[1]
            self._answer_callback(cb_id, "Quitando objetivo...")
            self._remove_by_index(chat_id, idx_str)
        else:
            self._answer_callback(cb_id)

    def _cmd_start(self, chat_id: str) -> None:
        targets = self.service.scheduler.targets
        targets_lines = "\n".join(
            f"  • <b>{html.escape(t.target_section)}</b> ({html.escape(t.event_url.split('/')[-1])})"
            for t in targets
        )
        auto_res = "✅ ACTIVA" if self.service.user_token else "⚪ Inactiva (sin cookie)"
        msg = (
            "🎸 <b>AllAccess Radar · Panel de Control (v2.1)</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Objetivos vigilados ({len(targets)}):</b>\n{targets_lines}\n\n"
            f"⏱️ <b>Frecuencia:</b> cada {self.service.config.check_interval_seconds}s\n"
            f"🛒 <b>Auto-Reserva Carrito:</b> {auto_res} (Cant: {self.service.reserve_quantity})\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Comandos disponibles:</b>\n"
            "• /chequear — Consulta en vivo ahora mismo\n"
            "• /reservar <code>[sector]</code> — Probar reserva REAL en AllAccess (ej: <code>/reservar Campo Delantero</code>)\n"
            "• /sectores — Ver todos los sectores y precios del show\n"
            "• /estado — Estadísticas y estado de sesión\n"
            "• /token — Ver o actualizar token de sesión de AllAccess\n"
            "• /agregar <code>&lt;sector&gt;</code> — Sumar otro sector a vigilar\n"
            "• /quitar — Quitar sectores con un toque\n"
            "• /test — Disparar simulacro de reserva (mock)"
        )
        self._send_message(chat_id, msg, reply_markup=self._main_keyboard())

    def _cmd_status(self, chat_id: str) -> None:
        targets = self.service.scheduler.targets
        last = self.service.last_result
        last_time = (
            datetime.fromtimestamp(self.service.last_check_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if self.service.last_check_epoch > 0
            else "Pendiente"
        )
        auto_res = (
            f"✅ <b>HABILITADA</b> ({self.service.reserve_quantity} entrada(s) con tu sesión)"
            if self.service.user_token
            else "⚪ <b>Deshabilitada</b> (falta ALLACCESS_USER_TOKEN)"
        )
        targets_list = "\n".join(
            f"  {i+1}. <b>{html.escape(t.target_section)}</b> — <code>{html.escape(t.event_url)}</code>"
            for i, t in enumerate(targets)
        )
        msg = (
            "📊 <b>Estado en Vivo · AllAccess Radar</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔄 <b>Chequeos realizados:</b> {self.service.total_checks}\n"
            f"🕒 <b>Último chequeo:</b> {last_time}\n"
            f"📡 <b>Último resultado:</b> <code>{last.status.value if last else 'N/A'}</code>\n"
            f"⏱️ <b>Intervalo:</b> cada {self.service.config.check_interval_seconds}s\n"
            f"🛒 <b>Auto-Reserva:</b> {auto_res}\n\n"
            f"🎯 <b>Sectores monitoreados ({len(targets)}):</b>\n{targets_list}"
        )
        self._send_message(chat_id, msg, reply_markup=self._main_keyboard())

    def _cmd_check(self, chat_id: str) -> None:
        results = self.service.scheduler.run_targets_tick()
        self.service.last_result = results[0] if results else None
        self.service.last_check_epoch = time.time()
        self.service.total_checks += 1

        lines: List[str] = [
            "🔍 <b>Resultado de Chequeo en Vivo</b>",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for res in results:
            icon = "🟢 <b>¡DISPONIBLE!</b>" if res.status == AvailabilityStatus.AVAILABLE else (
                "🔴 Agotado" if res.status == AvailabilityStatus.UNAVAILABLE else f"⚠️ {res.status.value}"
            )
            price_str = res.section_info.formatted_price if (res.section_info and res.section_info.price) else ""
            price_part = f" — 💰 {html.escape(price_str)}" if price_str else ""
            ev_name = html.escape(res.event_name or res.event_url.split("/")[-1])
            lines.append(
                f"• <b>{ev_name}</b> · <b>{html.escape(res.section_name)}</b>:\n"
                f"  {icon}{price_part}"
            )
            if res.status == AvailabilityStatus.AVAILABLE and res.direct_checkout_url:
                lines.append(f"  👉 <a href=\"{html.escape(res.direct_checkout_url)}\">Ir al Checkout directo</a>")

        lines.append(f"\n🕒 <i>Verificado: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}</i>")
        self._send_message(chat_id, "\n".join(lines), reply_markup=self._main_keyboard())

    def _cmd_sectors(self, chat_id: str) -> None:
        url = self.service.config.event_url
        try:
            resp = requests.get(url, headers=DEFAULT_BROWSER_HEADERS, timeout=15)
            marker = "App.bootstrapData("
            idx = resp.text.find(marker)
            if idx == -1:
                self._send_message(chat_id, "⚠️ No se pudo leer la grilla de sectores de AllAccess.")
                return
            data, _ = json.JSONDecoder().raw_decode(resp.text[idx + len(marker):])
            event_data = ((data or {}).get("model") or {}).get("data") or {}
            event_name = event_data.get("name") or "Foo Fighters"
            shows = event_data.get("shows") or []
            if not shows:
                self._send_message(chat_id, "⚠️ No se encontraron funciones en la página del evento.")
                return

            show = shows[0]
            sectors = show.get("sectors") or []
            watched_norms = {t.target_section.strip().lower() for t in self.service.scheduler.targets}

            lines = [
                f"🏟️ <b>Sectores en AllAccess: {html.escape(event_name)}</b>",
                "━━━━━━━━━━━━━━━━━━━━━━",
            ]
            buttons: List[List[Dict[str, str]]] = []
            row: List[Dict[str, str]] = []

            for sec in sectors:
                if not isinstance(sec, dict):
                    continue
                name = str(sec.get("name") or "").strip()
                if not name:
                    continue
                rates = [r for r in (sec.get("rates") or []) if isinstance(r, dict)]
                price = None
                srv = None
                avail = False
                for r in rates:
                    if price is None and r.get("price") is not None:
                        price = float(r["price"])
                        srv = float(r.get("serviceChargeAmount") or 0)
                    if r.get("available") and (r.get("max") or 0) > 0 and not r.get("showAsDisabled"):
                        avail = True

                status_badge = "🟢 DISP" if avail else "🔴 Agotado"
                watched_badge = " 👀" if name.lower() in watched_norms else ""
                price_txt = f"${price:,.0f} + ${srv:,.0f}" if price is not None else "S/D"
                lines.append(f"• <b>{html.escape(name)}</b>{watched_badge}: {status_badge} (<code>{price_txt}</code>)")

                if name.lower() not in watched_norms and len(name.encode("utf-8")) <= 48:
                    row.append({"text": f"➕ {name}", "callback_data": f"add_sec:{name}"})
                    if len(row) == 2:
                        buttons.append(row)
                        row = []

            if row:
                buttons.append(row)
            buttons.append([{"text": "🔙 Volver al Panel", "callback_data": "cmd:status"}])

            lines.append("\n💡 <i>Tocá un botón abajo para sumar ese sector al monitoreo en vivo:</i>")
            self._send_message(
                chat_id,
                "\n".join(lines),
                reply_markup={"inline_keyboard": buttons},
            )
        except Exception as exc:
            self._send_message(chat_id, f"⚠️ Error consultando sectores: <code>{html.escape(str(exc))}</code>")

    def _cmd_add(self, chat_id: str, arg: str) -> None:
        if not arg:
            self._send_message(
                chat_id,
                "ℹ️ <b>Cómo agregar un sector o evento:</b>\n\n"
                "1️⃣ <b>Otro sector de Foo Fighters:</b>\n"
                "<code>/agregar Campo delantero</code>\n\n"
                "2️⃣ <b>Otro evento de AllAccess:</b>\n"
                "<code>/agregar https://www.allaccess.com.ar/event/oasis | Campo</code>\n\n"
                "O tocá <b>🏟️ Sectores del Show</b> para agregar con un botón.",
                reply_markup=self._main_keyboard(),
            )
            return

        if "|" in arg:
            ev_url, sec_name = [p.strip() for p in arg.split("|", 1)]
        elif arg.startswith("http://") or arg.startswith("https://"):
            parts = arg.split(maxsplit=1)
            if len(parts) < 2:
                self._send_message(chat_id, "⚠️ Indicá también el sector. Ejemplo:\n<code>/agregar https://www.allaccess.com.ar/event/foo-fighters Sivori media</code>")
                return
            ev_url, sec_name = parts[0].strip(), parts[1].strip()
        else:
            ev_url = self.service.config.event_url
            sec_name = arg.strip()

        new_target = MonitoredTarget(event_url=ev_url, target_section=sec_name)
        current = list(self.service.scheduler.targets)
        for existing in current:
            if existing.target_id == new_target.target_id:
                self._send_message(
                    chat_id,
                    f"ℹ️ El sector <b>{html.escape(sec_name)}</b> ya estaba en tu lista vigilada.",
                    reply_markup=self._main_keyboard(),
                )
                return

        current.append(new_target)
        self.service.scheduler.targets = current
        self.service.config.targets = current
        self._send_message(
            chat_id,
            f"✅ <b>Objetivo agregado al monitoreo en vivo:</b>\n"
            f"• Sector: <b>{html.escape(sec_name)}</b>\n"
            f"• Evento: <code>{html.escape(ev_url)}</code>\n"
            f"• Total vigilados: <b>{len(current)}</b>",
            reply_markup=self._main_keyboard(),
        )

    def _cmd_manage_menu(self, chat_id: str) -> None:
        targets = list(self.service.scheduler.targets)
        buttons: List[List[Dict[str, str]]] = []
        for idx, t in enumerate(targets):
            ev_slug = t.event_url.rstrip("/").split("/")[-1]
            buttons.append([
                {
                    "text": f"🗑️ Quitar: {t.target_section} ({ev_slug})",
                    "callback_data": f"rem_idx:{idx}",
                }
            ])
        buttons.append([{"text": "🏟️ Ver Sectores para Agregar", "callback_data": "cmd:sectors"}])
        buttons.append([{"text": "🔙 Volver al Panel", "callback_data": "cmd:status"}])
        self._send_message(
            chat_id,
            "⚙️ <b>Gestionar Sectores Vigilados</b>\n"
            "Tocá un objetivo para quitarlo o sumá nuevos desde la lista del show:",
            reply_markup={"inline_keyboard": buttons},
        )

    def _cmd_remove(self, chat_id: str, arg: str) -> None:
        if not arg:
            self._cmd_manage_menu(chat_id)
            return
        targets = list(self.service.scheduler.targets)
        if len(targets) <= 1:
            self._send_message(chat_id, "⚠️ Debe quedar al menos 1 sector activo en el monitor.")
            return
        kept = [t for t in targets if t.target_section.strip().lower() != arg.strip().lower()]
        if len(kept) == len(targets):
            self._send_message(chat_id, f"⚠️ No se encontró el sector <b>{html.escape(arg)}</b>.")
            return
        self.service.scheduler.targets = kept
        self.service.config.targets = kept
        self._send_message(
            chat_id,
            f"🗑️ Se quitó <b>{html.escape(arg)}</b>. Quedan <b>{len(kept)}</b> objetivo(s) vigilados.",
            reply_markup=self._main_keyboard(),
        )

    def _remove_by_index(self, chat_id: str, idx_str: str) -> None:
        targets = list(self.service.scheduler.targets)
        if len(targets) <= 1:
            self._send_message(chat_id, "⚠️ Debe quedar al menos 1 sector activo en el monitor.")
            return
        if not idx_str.isdigit():
            return
        idx = int(idx_str)
        if 0 <= idx < len(targets):
            removed = targets.pop(idx)
            self.service.scheduler.targets = targets
            self.service.config.targets = targets
            self._send_message(
                chat_id,
                f"🗑️ Se quitó <b>{html.escape(removed.target_section)}</b>.\n"
                f"Quedan <b>{len(targets)}</b> objetivo(s) activos.",
                reply_markup=self._main_keyboard(),
            )

    def _cmd_test_reserve(self, chat_id: str) -> None:
        res_info = self.service.run_test_reserve()
        if res_info.get("status") == "ok":
            self._send_message(
                chat_id,
                f"✅ <b>Simulacro completado:</b> Carrito <code>{res_info.get('cart_id')}</code> generado.",
                reply_markup=self._main_keyboard(),
            )
        else:
            self._send_message(chat_id, "⚠️ El simulacro devolvió un error.", reply_markup=self._main_keyboard())

    def _cmd_live_reserve(self, chat_id: str, arg: str = "") -> None:
        sec_name = arg.strip() if arg else "Campo Delantero"
        self._send_message(
            chat_id,
            f"⏳ <i>Iniciando intento de reserva REAL en AllAccess para: <b>{html.escape(sec_name)}</b>...</i>\n"
            f"Resolviendo PoW y preparando orden...",
        )
        res_info = self.service.run_live_reserve(section_name=sec_name)
        status = res_info.get("status")

        if status == "ok":
            checkout_url = res_info.get("checkout_url") or "https://www.allaccess.com.ar/checkout"
            hold_sec = res_info.get("hold_seconds") or 600
            mins = int(hold_sec // 60)
            formatted_tot = res_info.get("formatted_total") or f"${res_info.get('total_amount', 0):,.0f} ARS"
            cart_id = res_info.get("cart_id") or "N/A"

            msg = (
                "🎉 <b>¡RESERVA REAL CONFIRMADA EN ALLACCESS!</b>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🏟️ <b>Sector:</b> {html.escape(sec_name)}\n"
                f"🎫 <b>Cantidad:</b> {self.service.reserve_quantity} entrada(s)\n"
                f"💰 <b>Total retenido:</b> {html.escape(formatted_tot)}\n"
                f"⏱️ <b>Tiempo para pagar:</b> {mins} minutos restantes\n"
                f"🆔 <b>ID de Reserva:</b> <code>{html.escape(cart_id)}</code>\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👉 <b><a href=\"{html.escape(checkout_url)}\">IR AL PAGO AHORA (LINK DIRECTO)</a></b>\n\n"
                "⚠️ <i>Hacé clic en el enlace para ingresar tus datos de tarjeta/pago antes de que expire la reserva.</i>"
            )
            keyboard = {
                "inline_keyboard": [
                    [{"text": "💳 Pagar en AllAccess Directo", "url": checkout_url}],
                    [{"text": "🔙 Panel Principal", "callback_data": "cmd:status"}],
                ]
            }
            self._send_message(chat_id, msg, reply_markup=keyboard)
        elif status == "no_token":
            self._send_message(
                chat_id,
                "⚠️ <b>Falta configurar tu sesión de AllAccess</b>\n\n"
                "Para poder reservar a tu nombre, el monitor necesita tu cookie o token de sesión.\n"
                "Podés configurarlo enviando:\n"
                "<code>/token &lt;tu_cookie_o_token&gt;</code>\n\n"
                "O en Render en la variable de entorno <code>ALLACCESS_USER_TOKEN</code>.",
                reply_markup=self._main_keyboard(),
            )
        elif status == "unavailable":
            self._send_message(
                chat_id,
                f"🔴 <b>Sector no disponible:</b>\n"
                f"El sector <b>{html.escape(sec_name)}</b> figura actualmente como <b>{res_info.get('ticket_status')}</b> en AllAccess.",
                reply_markup=self._main_keyboard(),
            )
        else:
            err = res_info.get("error_message") or "Error desconocido del servidor de AllAccess"
            res_status = res_info.get("reservation_status") or "FAILED"
            self._send_message(
                chat_id,
                f"⚠️ <b>No se pudo completar la reserva:</b>\n\n"
                f"• Estado: <code>{html.escape(str(res_status))}</code>\n"
                f"• Detalle: <code>{html.escape(str(err))}</code>\n\n"
                f"💡 <i>Si el error es 401 Unauthorized, tu sesión de AllAccess puede haber expirado. Renovala con /token.</i>",
                reply_markup=self._main_keyboard(),
            )

    def _cmd_token(self, chat_id: str, arg: str = "") -> None:
        if not arg:
            tok = self.service.user_token
            masked = (tok[:12] + "..." + tok[-8:]) if len(tok) > 24 else ("Configurado" if tok else "NO configurado")
            self._send_message(
                chat_id,
                f"🔑 <b>Sesión de AllAccess:</b> <code>{html.escape(masked)}</code>\n\n"
                "Para actualizar el token de sesión sin reiniciar Render, enviá:\n"
                "<code>/token &lt;tu_token_o_json_crowder&gt;</code>\n\n"
                "<i>Ejemplo: pegá el valor de localStorage.crowder o la cookie connect.sid</i>",
                reply_markup=self._main_keyboard(),
            )
            return

        clean_token = arg.strip()
        self.service.set_user_token(clean_token)
        masked = (clean_token[:12] + "..." + clean_token[-8:]) if len(clean_token) > 24 else clean_token
        self._send_message(
            chat_id,
            f"✅ <b>Token de sesión actualizado en memoria:</b>\n"
            f"<code>{html.escape(masked)}</code>\n\n"
            f"Auto-reserva lista para <b>{self.service.reserve_quantity}</b> entrada(s).\n"
            f"Podés probarla ahora con /reservar o tocando el botón abajo.",
            reply_markup=self._main_keyboard(),
        )
