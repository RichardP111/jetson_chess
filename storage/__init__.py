# storage/__init__.py
# Storage subsystem for the Jetson Smart Chess Board.
#
# Exports:
#   usb  — USBStorage singleton (plug-and-play USB drive access)
#
# Usage (anywhere in the project):
#   from storage import usb
#   usb.save_game(...)
#   usb.list_saved_games()
#   usb.load_game(filename)
#   usb.delete_game(filename)
#   usb.import_pgn(pgn_text, source_name)
#   usb.is_available()

from storage.usb_drive import USBStorage

usb = USBStorage()

__all__ = ["usb", "USBStorage"]