"""PyInstaller hook для ``polydoc``.

``polydoc.formats.readers`` и ``polydoc.formats.writers`` регистрируют форматы
через ``importlib.import_module(f".{name}", __name__)``, поэтому статический
анализатор PyInstaller не видит эти модули и в собранном бинарнике они
отсутствуют — polydoc пишет ``RuntimeWarning: the 'docx' reader is unavailable``
и не может открыть ни один документ.

Хук собирает все подмодули пакета, чтобы реестр форматов наполнялся и в
замороженном приложении.
"""

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("polydoc")
