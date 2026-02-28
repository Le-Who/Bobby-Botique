import re

with open('app/handlers/messages.py', 'r') as f:
    content = f.read()

# 1. Add import
if 'from app.utils.heartbeat import' not in content:
    # Find import app... and add ours
    content = content.replace(
        'from app.utils.text_format import split_text_safe',
        'from app.utils.heartbeat import register_heartbeat, stop_heartbeat, unregister_heartbeat\nfrom app.utils.text_format import split_text_safe'
    )

# 2. Modify _heartbeat to register and unregister, and add is_set check
old_heartbeat = """        done_event = asyncio.Event()

        async def _heartbeat() -> None:
            try:
                elapsed = 0
                for threshold, text in _WAIT_STAGES:
                    wait_for = threshold - elapsed
                    if wait_for <= 0:
                        continue
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=wait_for)
                        return  # Main task finished — stop heartbeat
                    except TimeoutError:
                        pass
                    elapsed = threshold
                    try:
                        await placeholder_message.edit_text(text)
                    except Exception:
                        pass  # Message already edited by main task or deleted
            except asyncio.CancelledError:
                pass  # Cleanly stop when task_wrapper cancels us"""

new_heartbeat = """        done_event = asyncio.Event()
        register_heartbeat(placeholder_message.message_id, done_event)

        async def _heartbeat() -> None:
            try:
                elapsed = 0
                for threshold, text in _WAIT_STAGES:
                    wait_for = threshold - elapsed
                    if wait_for <= 0:
                        continue
                    try:
                        await asyncio.wait_for(done_event.wait(), timeout=wait_for)
                        return  # Main task finished — stop heartbeat
                    except TimeoutError:
                        pass

                    if done_event.is_set():
                        return  # Stop heartbeat right before edit if task just finished

                    elapsed = threshold
                    try:
                        await placeholder_message.edit_text(text)
                    except Exception:
                        pass  # Message already edited by main task or deleted
            except asyncio.CancelledError:
                pass  # Cleanly stop when task_wrapper cancels us"""

content = content.replace(old_heartbeat, new_heartbeat)

# 3. Add unregister in finally of task_wrapper and error handling
old_task_wrapper_error = """            except Exception as e:
                logging.error(
                    "Error in task wrapper for user %s: %s", user_id, e, exc_info=True
                )
                try:
                    from app.errors import build_retry_and_roles_keyboard
                    await placeholder_message.edit_text(
                        "❌ Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
                        reply_markup=build_retry_and_roles_keyboard()
                    )
                except (BadRequest, NetworkError) as edit_error:"""

new_task_wrapper_error = """            except Exception as e:
                logging.error(
                    "Error in task wrapper for user %s: %s", user_id, e, exc_info=True
                )
                try:
                    stop_heartbeat(placeholder_message.message_id)
                    from app.errors import build_retry_and_roles_keyboard
                    await placeholder_message.edit_text(
                        "❌ Произошла ошибка при обработке запроса. Попробуйте ещё раз.",
                        reply_markup=build_retry_and_roles_keyboard()
                    )
                except (BadRequest, NetworkError) as edit_error:"""

content = content.replace(old_task_wrapper_error, new_task_wrapper_error)


old_import_error = """                        except ImportError:
                            # Fallback if agent недоступен
                            await placeholder_message.edit_text(
                                "🤔 Обрабатываю ваш запрос... (упрощенный режим)"
                            )"""

new_import_error = """                        except ImportError:
                            # Fallback if agent недоступен
                            stop_heartbeat(placeholder_message.message_id)
                            await placeholder_message.edit_text(
                                "🤔 Обрабатываю ваш запрос... (упрощенный режим)"
                            )"""

content = content.replace(old_import_error, new_import_error)

old_finally = """            finally:
                # Ensure heartbeat is stopped even on exception paths
                if not done_event.is_set():"""

new_finally = """            finally:
                # Ensure heartbeat is stopped even on exception paths
                unregister_heartbeat(placeholder_message.message_id)
                if not done_event.is_set():"""

content = content.replace(old_finally, new_finally)

with open('app/handlers/messages.py', 'w') as f:
    f.write(content)
print("Done patching messages.py")
