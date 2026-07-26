#-------------------------------------------------------------------
# Name: pdmpython.py 
# Desc: Terminal based Python client for viewing and editing source
#       members. Could be the basis for of a PASE based PDM replacement.
# Requirements:
# -Python 3.13 (May work with 3.9 but have not tested.)
# -Virtual environment that copies all Python packages. pyodbc is 
# part of the base packages for IBM i.
# -textual - pip3.13 install textual
# Links:
# https://textual.textualize.io
#-------------------------------------------------------------------
from __future__ import annotations

import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyodbc
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, Static, TextArea

IBM_NAME = re.compile(r"^[A-Z0-9_$#@]{1,10}$")


@dataclass(frozen=True)
class Member:
    name: str
    source_type: str
    text: str


@dataclass(frozen=True)
class SourceLocation:
    library: str
    source_file: str
    member: str

    @property
    def qualified_file(self) -> str:
        return f"{self.library}/{self.source_file}"


# Add or change source-type mappings here.
# {obj}, {srcfile}, and {member} are substituted at compile time.
COMPILE_COMMANDS: dict[str, str] = {
    "RPGLE": "CRTBNDRPG PGM({obj}) SRCFILE({srcfile}) SRCMBR({member}) DBGVIEW(*SOURCE)",
    "SQLRPGLE": "CRTSQLRPGI OBJ({obj}) SRCFILE({srcfile}) SRCMBR({member}) OBJTYPE(*PGM) DBGVIEW(*SOURCE)",
    "CLLE": "CRTBNDCL PGM({obj}) SRCFILE({srcfile}) SRCMBR({member}) DBGVIEW(*SOURCE)",
    "CLP": "CRTCLPGM PGM({obj}) SRCFILE({srcfile}) SRCMBR({member})",
    "CBLLE": "CRTBNDCBL PGM({obj}) SRCFILE({srcfile}) SRCMBR({member}) DBGVIEW(*SOURCE)",
    "SQLCBLLE": "CRTSQLCBLI OBJ({obj}) SRCFILE({srcfile}) SRCMBR({member}) OBJTYPE(*PGM) DBGVIEW(*SOURCE)",
    "C": "CRTBNDC PGM({obj}) SRCFILE({srcfile}) SRCMBR({member}) DBGVIEW(*SOURCE)",
    "CPP": "CRTBNDCPP PGM({obj}) SRCFILE({srcfile}) SRCMBR({member}) DBGVIEW(*SOURCE)",
    "CMD": "CRTCMD CMD({obj}) PGM({obj}) SRCFILE({srcfile}) SRCMBR({member})",
    "DSPF": "CRTDSPF FILE({obj}) SRCFILE({srcfile}) SRCMBR({member})",
    "PRTF": "CRTPRTF FILE({obj}) SRCFILE({srcfile}) SRCMBR({member})",
    "PF": "CRTPF FILE({obj}) SRCFILE({srcfile}) SRCMBR({member})",
    "LF": "CRTLF FILE({obj}) SRCFILE({srcfile}) SRCMBR({member})",
    "MENU": "CRTMNU MENU({obj}) SRCFILE({srcfile}) SRCMBR({member})",
}


class IbmiService:
    """IBM i database and CL-command operations."""

    def __init__(self) -> None:
        # Override this with a full unixODBC/IBM i Access connection string.
        # When running in IBM i PASE, DSN=*LOCAL is a common local setup.
        connection_string = os.getenv("IBMI_ODBC_CONNECTION_STRING", "DSN=*LOCAL;")
        self.connection = pyodbc.connect(connection_string, autocommit=False)

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def validate_name(value: str, label: str) -> str:
        value = value.strip().upper()
        if not IBM_NAME.fullmatch(value):
            raise ValueError(f"{label} must be a valid 1-10 character IBM i system name.")
        return value

    def members(self, library: str, source_file: str) -> list[Member]:
        library = self.validate_name(library, "Library")
        source_file = self.validate_name(source_file, "Source file")
        sql = """
            SELECT TRIM(SYSTEM_TABLE_MEMBER),
                   COALESCE(TRIM(SOURCE_TYPE), ''),
                   COALESCE(CAST(PARTITION_TEXT AS VARCHAR(50)), '')
              FROM QSYS2.SYSPARTITIONSTAT
             WHERE SYSTEM_TABLE_SCHEMA = ?
               AND SYSTEM_TABLE_NAME = ?
               AND SOURCE_TYPE IS NOT NULL
             ORDER BY SYSTEM_TABLE_MEMBER
        """
        cursor = self.connection.cursor()
        try:
            cursor.execute(sql, (library, source_file))
            return [Member(str(r[0]).strip(), str(r[1]).strip(), str(r[2]).strip()) for r in cursor.fetchall()]
        finally:
            cursor.close()

    def run_cl(self, command: str) -> str:
        """Run a CL command through the IBM i PASE system utility."""
        process = subprocess.run(
            ["/QOpenSys/usr/bin/system", "-i", command],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
        output = process.stdout.strip()
        if process.returncode != 0:
            raise RuntimeError(output or f"CL command failed with return code {process.returncode}")
        return output

    def export_member(self, location: SourceLocation, target: Path) -> None:
        # ENDLINFMT(*LF) gives TextArea normal Unix line endings.
        command = (
            f"CPYTOSTMF FROMMBR('/QSYS.LIB/{location.library}.LIB/"
            f"{location.source_file}.FILE/{location.member}.MBR') "
            f"TOSTMF({self._cl_quote(str(target))}) STMFOPT(*REPLACE) "
            "STMFCCSID(1208) ENDLINFMT(*LF)"
        )
        self.run_cl(command)

    def import_member(self, location: SourceLocation, source: Path) -> None:
        # MBROPT(*REPLACE) guarantees that save returns to the original member.
        command = (
            f"CPYFRMSTMF FROMSTMF({self._cl_quote(str(source))}) "
            f"TOMBR('/QSYS.LIB/{location.library}.LIB/"
            f"{location.source_file}.FILE/{location.member}.MBR') "
            "MBROPT(*REPLACE) STMFCCSID(1208)"
        )
        self.run_cl(command)

    def read_member(self, location: SourceLocation) -> str:
        path = Path(tempfile.gettempdir()) / f"ibmi_src_{os.getpid()}_{location.member}.txt"
        try:
            self.export_member(location, path)
            return path.read_text(encoding="utf-8", errors="replace")
        finally:
            path.unlink(missing_ok=True)

    def save_member(self, location: SourceLocation, content: str) -> None:
        path = Path(tempfile.gettempdir()) / f"ibmi_src_{os.getpid()}_{location.member}.txt"
        try:
            path.write_text(content, encoding="utf-8", newline="\n")
            self.import_member(location, path)
        finally:
            path.unlink(missing_ok=True)

    def compile(self, location: SourceLocation, source_type: str, object_library: str) -> tuple[str, str]:
        object_library = self.validate_name(object_library, "Object library")
        template = COMPILE_COMMANDS.get(source_type.upper())
        if not template:
            raise ValueError(f"No compiler mapping is defined for source type {source_type or '<blank>'}.")
        command = template.format(
            obj=f"{object_library}/{location.member}",
            srcfile=location.qualified_file,
            member=location.member,
        )
        return command, self.run_cl(command)

    @staticmethod
    def _cl_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"


class MessageScreen(ModalScreen[None]):
    def __init__(self, title: str, message: str) -> None:
        super().__init__()
        self.title_text = title
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="message-dialog"):
            yield Label(self.title_text, id="message-title")
            yield Static(self.message or "Command completed successfully.", id="message-body")
            yield Button("Close", variant="primary", id="close")

    @on(Button.Pressed, "#close")
    def close_dialog(self) -> None:
        self.dismiss(None)


class SourceScreen(Screen[None]):
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Back", show=True),
    ]

    def __init__(self, service: IbmiService, location: SourceLocation, browse_only: bool) -> None:
        super().__init__()
        self.service = service
        self.location = location
        self.browse_only = browse_only
        self.original_text = ""

    def compose(self) -> ComposeResult:
        mode = "Browse" if self.browse_only else "Edit"
        yield Header()
        yield Label(
            f"{mode}: {self.location.library}/{self.location.source_file}({self.location.member})",
            id="editor-title",
        )
        yield TextArea(id="source", read_only=self.browse_only, show_line_numbers=True, language=None)
        with Horizontal(id="editor-buttons"):
            if not self.browse_only:
                yield Button("Save", variant="success", id="save")
            yield Button("Back", id="cancel")
        yield Footer()

    def on_mount(self) -> None:
        try:
            self.original_text = self.service.read_member(self.location)
            self.query_one("#source", TextArea).load_text(self.original_text)
            self.query_one("#source", TextArea).focus()
        except Exception as exc:
            self.app.push_screen(MessageScreen("Unable to open member", str(exc)))

    def action_save(self) -> None:
        if self.browse_only:
            return
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#save")
    def save_pressed(self) -> None:
        self._save()

    @on(Button.Pressed, "#cancel")
    def cancel_pressed(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        try:
            content = self.query_one("#source", TextArea).text
            self.service.save_member(self.location, content)
            self.original_text = content
            self.app.push_screen(
                MessageScreen(
                    "Saved",
                    f"Saved back to {self.location.library}/{self.location.source_file}({self.location.member}).",
                )
            )
        except Exception as exc:
            self.app.push_screen(MessageScreen("Save failed", str(exc)))


class SourceMemberApp(App[None]):
    TITLE = "IBM i Source Member TUI"
    CSS = """
    Screen { layout: vertical; }
    #criteria { height: auto; padding: 1; }
    #criteria Input { width: 20; margin-right: 1; }
    #member-table { height: 1fr; }
    #option-bar { height: auto; padding: 1; }
    #option { width: 10; margin-right: 1; }
    #status { height: 1; padding-left: 1; }
    #editor-title { height: 3; padding: 1; text-style: bold; }
    #source { height: 1fr; }
    #editor-buttons { height: 3; padding-left: 1; }
    #editor-buttons Button { margin-right: 1; }
    MessageScreen { align: center middle; }
    #message-dialog { width: 80%; height: 70%; border: round $accent; padding: 1 2; background: $surface; }
    #message-title { height: 2; text-style: bold; }
    #message-body { height: 1fr; overflow-y: auto; }
    #close { width: 16; }
    """
    BINDINGS = [
        Binding("f5", "load", "Load", show=True),
        Binding("e", "view_edit", "Edit", show=True),
        Binding("b", "browse", "Browse", show=True),
        Binding("c", "compile", "Compile", show=True),
        ##Binding("f4", "compile", "14 Compile", show=True),
        Binding("ctrl+q", "quit", "Quit", show=True, priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.service: IbmiService | None = None
        self.member_by_key: dict[object, Member] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="criteria"):
            yield Label("Library:")
            yield Input(placeholder="QSHONI", id="library", max_length=10)
            yield Label("Source file:")
            yield Input(value="QRPGLESRC", id="source-file", max_length=10)
            yield Label("Object library:")
            yield Input(placeholder="same as library", id="object-library", max_length=10)
            yield Button("Load", variant="primary", id="load")
        yield DataTable(id="member-table", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="option-bar"):
            ##yield Label("Option (E, B, 14):")
            yield Label("Option (E, B, C):")
            yield Input(id="option", max_length=2)
            yield Button("Run", id="run-option", variant="success")
        yield Static("Enter a library and source file, then press F5 or Load.", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#member-table", DataTable)
        table.add_columns("Opt", "Member", "Type", "Text")
        try:
            self.service = IbmiService()
            self.query_one("#library", Input).focus()
        except Exception as exc:
            self.push_screen(MessageScreen("IBM i connection failed", str(exc)))

    def on_unmount(self) -> None:
        if self.service:
            self.service.close()

    @on(Button.Pressed, "#load")
    def load_pressed(self) -> None:
        self.action_load()

    @on(Button.Pressed, "#run-option")
    def run_option_pressed(self) -> None:
        self.run_entered_option()

    @on(Input.Submitted, "#option")
    def option_submitted(self) -> None:
        self.run_entered_option()

    def action_load(self) -> None:
        if not self.service:
            return
        try:
            library, source_file = self.current_file()
            members = self.service.members(library, source_file)
            table = self.query_one("#member-table", DataTable)
            table.clear()
            self.member_by_key.clear()
            for member in members:
                key = table.add_row("", member.name, member.source_type, member.text)
                self.member_by_key[key] = member
            self.set_status(f"Loaded {len(members)} members from {library}/{source_file}.")
            if members:
                table.focus()
        except Exception as exc:
            self.push_screen(MessageScreen("Load failed", str(exc)))

    def action_view_edit(self) -> None:
        self.open_selected(browse_only=False)

    def action_browse(self) -> None:
        self.open_selected(browse_only=True)

    def action_compile(self) -> None:
        self.compile_selected()

    def run_entered_option(self) -> None:
        option_input = self.query_one("#option", Input)
        option = option_input.value.strip().upper()
        option_input.value = ""
        if option == "E":
            self.open_selected(browse_only=False)
        elif option == "B":
            self.open_selected(browse_only=True)
        elif option == "C":
            self.compile_selected()
        else:
            self.push_screen(MessageScreen("Invalid option", "Use V to edit, B to browse, or 14 to compile."))

    def selected_member(self) -> Member:
        table = self.query_one("#member-table", DataTable)
        if table.row_count == 0 or table.cursor_row < 0:
            raise ValueError("Select a source member first.")
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        member = self.member_by_key.get(row_key)
        if not member:
            raise ValueError("Unable to identify the selected member.")
        return member

    def current_file(self) -> tuple[str, str]:
        assert self.service is not None
        library = self.service.validate_name(self.query_one("#library", Input).value, "Library")
        source_file = self.service.validate_name(self.query_one("#source-file", Input).value, "Source file")
        return library, source_file

    def selected_location(self) -> tuple[SourceLocation, Member]:
        library, source_file = self.current_file()
        member = self.selected_member()
        return SourceLocation(library, source_file, member.name), member

    def open_selected(self, browse_only: bool) -> None:
        if not self.service:
            return
        try:
            location, _ = self.selected_location()
            # Callback-style push avoids Textual's wait_for_dismiss worker requirement.
            self.push_screen(SourceScreen(self.service, location, browse_only))
        except Exception as exc:
            self.push_screen(MessageScreen("Unable to open member", str(exc)))

    def compile_selected(self) -> None:
        if not self.service:
            return
        try:
            location, member = self.selected_location()
            object_library_value = self.query_one("#object-library", Input).value.strip()
            object_library = object_library_value or location.library
            command, output = self.service.compile(location, member.source_type, object_library)
            body = f"Command:\n{command}\n\nOutput:\n{output or 'Compile command completed.'}"
            self.push_screen(MessageScreen("Compile result", body))
            self.set_status(f"Compiled {member.name} using {member.source_type} mapping.")
        except Exception as exc:
            self.push_screen(MessageScreen("Compile failed", str(exc)))

    def set_status(self, message: str) -> None:
        self.query_one("#status", Static).update(message)


if __name__ == "__main__":
    SourceMemberApp().run()
