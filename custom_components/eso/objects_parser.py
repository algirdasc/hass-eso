"""HTML parser for discovering ESO objects (metering points).

After login, the consumption page (``/consumption``) renders a
``<select name="objects[]">`` whose ``<option>`` values are the numeric object
IDs and whose text is the address followed by the meter number, e.g.
``Atsitiktine g. 25-14, 36237 Vilnius, 12222222``. We use this select as the
authoritative source of IDs and derive the display name by stripping the
trailing meter number from the option label.
"""

import re
from html.parser import HTMLParser


class SelectObjectsParser(HTMLParser):
    """Collect ``id -> label`` from the ``objects[]`` select on /consumption."""

    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[str, str] = {}
        self._in_objects_select: bool = False
        self._current_value: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        attributes = dict(attrs)
        if tag == "select" and attributes.get("name") == "objects[]":
            self._in_objects_select = True
            return
        if tag == "option" and self._in_objects_select:
            self._current_value = attributes.get("value")
            self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._in_objects_select and self._current_value is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "option" and self._in_objects_select and self._current_value:
            text = " ".join("".join(self._current_text).split())
            if self._current_value.isdigit() and text:
                self.objects[self._current_value] = text
            self._current_value = None
            self._current_text = []
        elif tag == "select" and self._in_objects_select:
            self._in_objects_select = False


def clean_object_name(label: str) -> str:
    """Derive the address object address from a select option label.

    The select label is ``<address>, <postcode> <city>, <meter number>``; the
    trailing meter number is stripped to leave the clean address.
    """
    return re.sub(r",\s*\d+\s*$", "", label).strip()
