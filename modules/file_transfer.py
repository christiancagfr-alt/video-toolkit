"""File transfers for mounted/virtual volumes without Windows CopyFile2."""
import os
import shutil
from pathlib import Path


def filesystem_path(path):
    value = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def transfer_file(source, destination, *, move=False, warn=lambda message: None):
    source, destination = filesystem_path(source), filesystem_path(destination)
    if os.name == "nt" and move:
        # Windows rename does not replace an existing destination. Keep fast
        # same-volume moves; other volumes/drivers can use the streaming path.
        try:
            os.rename(source, destination)
            return
        except FileExistsError:
            raise
        except OSError:
            pass
    created = False
    try:
        with source.open("rb") as reader:
            before = os.fstat(reader.fileno())
            # Exclusive creation also prevents overwrites if a name appeared
            # after the batch's collision check.
            with destination.open("xb") as writer:
                created = True
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                after = os.fstat(reader.fileno())
                if (before.st_size != os.fstat(writer.fileno()).st_size
                        or (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                    raise OSError("复制校验失败：源文件在复制期间发生变化或目标文件不完整")
    except Exception:
        if created:
            try:
                destination.unlink()
            except OSError as cleanup_error:
                warn(f"未能删除未完成文件，请检查：{destination}（{cleanup_error}）")
        raise
    try:
        shutil.copystat(source, destination)
    except OSError as exc:
        warn(f"文件内容已完整复制，但未能保留全部文件属性：{destination.name}（{exc}）")
    if move:
        try:
            source.unlink()
        except OSError as exc:
            raise OSError(f"目标文件已完整复制，但无法删除源文件；两份均已保留。目标：{destination}") from exc
