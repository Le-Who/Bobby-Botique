import os
import logging
import sys

# fcntl доступен только на Unix-подобных системах
try:
    import fcntl
    HAS_FCNTL = True
except ImportError:
    HAS_FCNTL = False

class ProcessLock:
    def __init__(self):
        self.lock_file = None
        self.lock_fd = None

    def acquire(self):
        """Приобретает блокировку файла для предотвращения множественных экземпляров"""

        # На Windows без fcntl используем упрощенную проверку
        if not HAS_FCNTL:
            logging.warning("fcntl not available (Windows detected). Using simplified lock mechanism.")
            container_id = os.environ.get('HOSTNAME', 'unknown')
            self.lock_file = os.path.join(os.path.expanduser("~"), f"gemaibot.{container_id}.lock")

            # Проверяем, существует ли файл блокировки
            if os.path.exists(self.lock_file):
                try:
                    # Читаем PID из файла
                    with open(self.lock_file, 'r') as f:
                        pid_str = f.read().strip()
                        if pid_str.isdigit():
                            pid = int(pid_str)
                            # Проверяем, существует ли процесс
                            try:
                                os.kill(pid, 0)  # Проверка существования процесса
                                logging.warning(f"Process {pid} is still running - lock is active")
                                return False
                            except (OSError, ProcessLookupError):
                                # Процесс не существует - удаляем старый lock
                                logging.info(f"Process {pid} is not running - removing stale lock")
                                os.unlink(self.lock_file)
                except Exception as e:
                    logging.warning(f"Error checking lock file: {e}")
                    # Пытаемся удалить файл, если он поврежден
                    try:
                        os.unlink(self.lock_file)
                    except:
                        pass

            # Создаем новый файл блокировки
            try:
                with open(self.lock_file, 'w') as f:
                    f.write(str(os.getpid()))
                logging.info(f"Lock acquired successfully (Windows mode). PID: {os.getpid()}, Container: {container_id}")
                return True
            except Exception as e:
                logging.error(f"Failed to acquire lock (Windows mode): {e}")
                return False

        # Unix-подобные системы - используем fcntl
        try:
            # Упрощенная логика для контейнерной среды
            container_id = os.environ.get('HOSTNAME', 'unknown')
            self.lock_file = f"/tmp/gemaibot.{container_id}.lock"

            # В контейнерной среде всегда удаляем старые блокировки
            if os.path.exists(self.lock_file):
                try:
                    os.unlink(self.lock_file)
                    logging.info(f"Removed existing lock file for container {container_id}")
                except Exception as e:
                    logging.warning(f"Error removing existing lock: {e}")

            # Создаем новый файл блокировки
            self.lock_fd = os.open(self.lock_file, os.O_CREAT | os.O_RDWR)

            # Пытаемся приобрести эксклюзивную блокировку
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # Записываем PID текущего процесса
            pid = str(os.getpid())
            os.write(self.lock_fd, pid.encode())
            os.fsync(self.lock_fd)

            logging.info(f"Lock acquired successfully. PID: {pid}, Container: {container_id}")
            return True

        except (OSError, IOError) as e:
            if self.lock_fd:
                try:
                    os.close(self.lock_fd)
                except:
                    pass
                self.lock_fd = None

            logging.error(f"Failed to acquire lock: {e}")
            return False

    def release(self):
        """Освобождает блокировку файла"""
        try:
            # На Windows просто удаляем файл
            if not HAS_FCNTL:
                if self.lock_file and os.path.exists(self.lock_file):
                    try:
                        os.unlink(self.lock_file)
                        logging.info("Lock file removed successfully (Windows mode)")
                    except Exception as e:
                        logging.warning(f"Error removing lock file (Windows mode): {e}")
                self.lock_file = None
                return

            # Unix-подобные системы - используем fcntl
            if self.lock_fd:
                try:
                    fcntl.flock(self.lock_fd, fcntl.LOCK_UN)
                    os.close(self.lock_fd)
                except (OSError, IOError) as e:
                    logging.warning(f"Error releasing file lock: {e}")
                finally:
                    self.lock_fd = None

            if self.lock_file and os.path.exists(self.lock_file):
                try:
                    os.unlink(self.lock_file)
                    logging.info("Lock file removed successfully")
                except (OSError, IOError) as e:
                    logging.warning(f"Error removing lock file: {e}")

        except Exception as e:
            logging.error(f"Error releasing lock: {e}")
        finally:
            self.lock_file = None
            if HAS_FCNTL:
                self.lock_fd = None

# Singleton instance
process_lock = ProcessLock()
