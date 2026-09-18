"""Reproducible synthetic fixtures; run with `python examples/measure.py`."""

import json

from rspns import Summarizer


def fixtures():
    return {
        "html_css": '<html><style>' + '.box{color:red;margin:10px;}' * 1000
        + '</style><body><form action="/login"><input name="user"><button>Login</button></form></body></html>',
        "json_long_strings": json.dumps([
            {"description": "lorem ipsum " * 1000, "action": "/api/" + str(i)} for i in range(20)
        ]),
        "xml_long_strings": '<?xml version="1.0"?><rows>' + ''.join(
            '<row id="' + str(i) + '"><description>' + 'lorem ipsum ' * 1000 + '</description></row>'
            for i in range(20)
        ) + '</rows>',
        "javascript_payload": 'const payload = "' + 'x' * 100000
        + '"; function submit(){return fetch("/api");}',
        "json_short_rows": json.dumps([
            {"action": "/api/" + str(i), "enabled": True} for i in range(100)
        ]),
    }


if __name__ == "__main__":
    print("fixture,input_characters,output_characters,output_over_input")
    for name, source in fixtures().items():
        output = Summarizer().summarize(source)
        size = len(output)
        print(f"{name},{len(source)},{size},{size / len(source):.3f}")
