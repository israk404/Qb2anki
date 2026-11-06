# Contributing

Thank you for helping improve QB to Anki.

## Reporting a problem

Open an issue and include:

- what you did and what you expected;
- the message shown in the app, or the relevant lines from `data\qb.log`;
- if possible, a debug bundle (**Database → Make a debug bundle**). It contains your cards and page crops but never your API keys;
  only attach it if you are comfortable sharing that content.

## Development setup

```bash
git clone https://github.com/israk404/qb2anki.git
cd qb2anki
python -m venv .venv
.venv\Scripts\activate          # Windows; on macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
pip install anki pyflakes        # optional: real Anki import tests, linting
python -m unittest discover -s tests
python -m qb2anki                # opens the app
```

Python 3.11 to 3.13 are supported.

## Rules for changes

- **Run the tests** before and after a change, and add a test for every bug you fix.
- **Reading prompts are part of the answer cache.** Changing `page_prompt` or `general_page_prompt` in `prompts.py` makes every
  saved reading invalid, so users pay requests again to rebuild. Only change them with a good reason, and say so in the changelog.
- **Keep the database compatible.** Add new columns through `DB._ensure_columns`; never rename or drop existing ones.
- **AI output is never silent.** Anything the AI adds or changes must be labelled on the card or flagged, and the original kept.
- **Never log or store API keys.** Show at most their last four characters.
- Keep dependencies minimal; the app itself uses only the standard library plus `google-genai`, `pillow` and `pydantic`.

## Pull requests

Keep a pull request to one topic, describe what changed and why, and update `CHANGELOG.md` and the user guide when behaviour
changes.
