from html.parser import HTMLParser


class FormParser(HTMLParser):
    form: dict = {}

    def handle_starttag(self, tag: str, attrs: tuple) -> None:
        if tag != "input":
            return

        self.handle_input_tag(attrs)

    def get(self, attribute: str) -> dict | None:
        if attribute not in self.form:
            return None

        return self.form[attribute]

    def handle_input_tag(self, attrs: tuple) -> None:
        attributes = dict(attrs)

        if "name" in attributes and attributes["name"] in ["form_token", "form_build_id", "form_id"]:
            self.form[attributes["name"]] = attributes["value"]
