# /app/handlers/msg_document.py
"""Document upload and Q&A handlers extracted from messages.py.

Manages: document uploads (PDF/DOCX), duplicate detection, document
mode interaction, and document-context Q&A.
"""

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.document_processor import process_uploaded_document
from app.i18n import t
from app.metrics import metrics_collector
from app.repos.chats import get_user_chat
from app.utils.formatting import TelegramFormatter


async def handle_document_mode_interaction(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    """Handle text input while user is in document mode. Returns True if consumed."""
    from app.state import get_selected_document_id, is_in_document_mode

    if is_in_document_mode(user_id):
        document_id = get_selected_document_id(user_id)
        logging.info("User %s is in document mode, document_id: %s", user_id, document_id)
        if document_id:
            await handle_document_question(update, context, document_id)
        else:
            await update.message.reply_text(
                "📋 Вы находитесь в режиме работы с документами.\n\n"
                "💡 *Доступные действия:*\n"
                "• Загрузите новый документ\n"
                "• Выберите документ из списка\n"
                "• Используйте кнопки под сообщениями\n\n"
                "🔄 *Для выхода из режима документов:*\n"
                "• Нажмите кнопку '❌ Отменить работу с документами'\n"
                "• Или отправьте команду /documents"
            )
        return True
    return False


async def handle_document_question(update: Update, context: ContextTypes.DEFAULT_TYPE, document_id: int) -> None:
    """Process a user's question about a specific document."""
    user_id = update.effective_user.id
    user_message = update.message.text

    try:
        from app.document_processor import get_document_by_id, get_document_content

        document = await get_document_by_id(document_id, user_id)
        if not document:
            await update.message.reply_text(
                "❌ Документ не найден.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t("doc.to_documents"), callback_data="open_documents")]]
                ),
            )
            from app.state import clear_document_state

            clear_document_state(user_id)
            return

        document_content = await get_document_content(document_id, user_id)
        if not document_content:
            await update.message.reply_text(
                "❌ Не удалось получить содержимое документа.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(t("doc.to_documents"), callback_data="open_documents")]]
                ),
            )
            return

        from app.handlers.agent import _handle_document_question

        chat_state = await get_user_chat(user_id)
        await _handle_document_question(update.message, user_id, user_message or "", chat_state)  # type: ignore[arg-type]  # message exists in this path

    except Exception as e:
        logging.error("Error handling document question: %s", e, exc_info=True)
        await update.message.reply_text(
            t("doc.error_question"),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton(t("doc.to_documents"), callback_data="open_documents")]]
            ),
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle uploaded documents (PDF, DOCX)."""
    user_id = update.effective_user.id

    if not update.message.document:
        return

    document = update.message.document

    # Check file size (max 50MB)
    if document.file_size and document.file_size > 50 * 1024 * 1024:
        await update.message.reply_text(
            "❌ Файл слишком большой. Максимальный размер: 50MB.\nПопробуйте файл меньшего размера."
        )
        return

    # Check file type
    supported_formats = [".pdf", ".docx", ".doc"]
    file_ext = document.file_name.lower().split(".")[-1] if document.file_name and "." in document.file_name else ""

    if f".{file_ext}" not in supported_formats:
        await update.message.reply_text(f"❌ Неподдерживаемый формат файла `.{file_ext}`.\nОтправьте PDF или DOCX.")
        return

    processing_msg = await update.message.reply_text("📄 Обрабатываю документ...")

    try:
        import os
        import tempfile

        file = await document.get_file()
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=f".{file_ext}")
        os.close(tmp_fd)

        try:
            await file.download_to_drive(custom_path=tmp_path)
            result = await process_uploaded_document(
                tmp_path,
                document.file_name or f"document.{file_ext}",
                user_id,
                is_path=True,
            )
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError as cleanup_error:
                    logging.warning(
                        "Failed to cleanup temp doc file %s: %s",
                        tmp_path,
                        cleanup_error,
                    )

        if result.get("error"):
            if result.get("error") == "duplicate":
                duplicate_info = result.get("duplicate_info", {})
                created_date = duplicate_info.get("created_at", "Unknown")
                if hasattr(created_date, "strftime"):
                    date_str = created_date.strftime("%Y-%m-%d")
                else:
                    date_str = str(created_date)[:10] if created_date != "Unknown" else "Unknown"

                duplicate_text = (
                    f"⚠️ *Файл уже загружен*\n\n"
                    f"Файл `{document.file_name}` уже был загружен ранее как:\n"
                    f"📄 *{duplicate_info.get('filename', 'Unknown')}*\n"
                    f"📅 Загружен: {date_str}\n\n"
                    f"Хотите использовать существующий документ?"
                )

                keyboard = [
                    [
                        InlineKeyboardButton(
                            "✅ Использовать существующий",
                            callback_data=f"doc:use_existing:{duplicate_info.get('id')}",
                        )
                    ],
                    [InlineKeyboardButton("📄 Загрузить как новый", callback_data="doc:force_upload")],
                    [InlineKeyboardButton("❌ Отмена", callback_data="doc:cancel")],
                ]

                formatted_text, parse_mode = TelegramFormatter.format_text(duplicate_text)
                await processing_msg.edit_text(
                    formatted_text,
                    parse_mode=parse_mode,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                )
                return
            else:
                logging.warning("Document processing error: %s", result["error"])
                await processing_msg.edit_text("❌ Не удалось обработать документ. Попробуйте другой файл.")
                return

        from app.document_processor import document_processor

        user_stats = await document_processor.get_user_document_stats(user_id)

        success_text = (
            f"✅ Документ обработан успешно!\n\n"
            f"📄 *{document.file_name}*\n"
            f"📊 Страниц: {result.get('pages', 'N/A')}\n"
            f"📝 Символов: {result.get('text_length', 0):,}\n"
        )

        if result.get("paragraphs"):
            success_text += f"📄 Параграфов: {result['paragraphs']}\n"
        if result.get("tables"):
            success_text += f"📊 Таблиц: {result['tables']}\n"

        success_text += f"\n📋 *Ваши документы:* {user_stats['document_count']}/5\n"
        if user_stats["limit_reached"]:
            success_text += "⚠️ Достигнут лимит документов (5). Старые документы будут автоматически удалены.\n"

        success_text += '\n💡 *Как задавать вопросы:*\n• Просто напишите ваш вопрос\n• Например: "Какие основные пункты?", "Что говорится о...?"\n• Система автоматически найдет ответ в документе\n\n'
        success_text += "📅 *Срок хранения:* 3 дня (автоматическая очистка)"

        keyboard = [
            [InlineKeyboardButton("📄 Загрузить другой документ", callback_data="doc:upload_new")],
            [InlineKeyboardButton("📋 Выбрать документ", callback_data="doc:select_document")],
            [InlineKeyboardButton("❌ Отменить работу с документами", callback_data="doc:cancel")],
        ]

        formatted_text, parse_mode = TelegramFormatter.format_text(success_text)
        await processing_msg.edit_text(
            formatted_text,
            parse_mode=parse_mode,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

        from app.state import set_document_mode

        set_document_mode(user_id, True)
        await metrics_collector.record_api_call("document_processing")

        # ── Store document summary in long-term memory (background) ──
        doc_content = result.get("content", "")
        if doc_content and len(doc_content) > 100:
            from app.utils.background_tasks import submit_retryable

            _doc_bytes = doc_content.encode("utf-8", errors="replace")
            _doc_uid = user_id

            def _bg_doc_ltm():
                async def _store():
                    from app.utils.multimodal_processor import process_media_for_memory

                    await process_media_for_memory(
                        _doc_bytes,
                        _doc_uid,
                        media_type="document_text",
                    )

                return _store()

            submit_retryable(_bg_doc_ltm, retry=2)

    except Exception as e:
        logging.error("Error processing document for user %s: %s", user_id, e, exc_info=True)
        from app.utils.keyboards import error_with_back_keyboard

        await processing_msg.edit_text(
            t("doc.error_processing"),
            reply_markup=error_with_back_keyboard("open_documents", t("doc.to_documents")),
        )
        await metrics_collector.record_error("document_processing", str(e))
