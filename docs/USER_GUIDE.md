# QB to Anki — User guide

Turns **photos and screenshots of MCQ question banks** into **Anki flashcards**. You control everything from one app in your
browser; nothing runs in the cloud except the reading itself, which is done by Google's Gemini AI with your own key.

What it does for you:

- works out by itself whether a photo is a book spread, a single page or a screenshot, and turns it upright;
- reads Bangla and English questions, options, answers, explanations and math (including figures);
- **proofreads every page against the photo**: fixes misread letters and digits (Bengali ৪ read as 8, …), broken formulas and
  ditto marks, and keeps the first reading so you can compare;
- finds the answer key, the answer printed next to each question, or an answer line at the bottom of the page;
- **works out missing answers and writes missing explanations with AI**, and clearly labels them;
- **checks math**: tests every formula, repairs broken formatting, and reads a picture again when a solution is still broken;
- merges **duplicate questions** across exams and remembers every exam a question appeared in;
- holds back anything it is unsure about until you approve it, so mistakes do not reach Anki silently;
- creates one file you import into Anki, with a study-friendly card design (light and night mode);
- backs up your cards automatically before every task.

---

## 1. One-time setup (about 10 minutes)

1. **Install Python 3.12** from python.org (64-bit). 3.11 and 3.13 also work. **Do not use 3.14 or newer**: some packages here have
   compiled parts that do not exist for those versions yet. You can keep another Python installed next to it.
2. **Unzip this folder** somewhere simple, for example `C:\QB2Anki` (a short path without spaces is the safest).
3. Double-click **setup.bat**. It creates a private Python environment (`.venv`) and installs what the app needs. It takes a few minutes.
4. Double-click **start.bat**. A black window opens (keep it open while you use the app) and the app opens in your browser at
   `http://127.0.0.1:8765`.
5. In the app open **Settings → Google AI key**, paste your key and press **Save key**. Get a free key at
   https://aistudio.google.com/apikey.
6. Press **Check setup**. Every model line should say OK. If a model says "not found" or "no longer available", go to
   **Settings → Models → Find working models**, then **Use these**.

That is all. From now on you only need **start.bat**.

---

## 2. Every day

| Step | Where | What you do |
|---|---|---|
| 1 | **Read photos** | Drop your photos in (or copy them into the `inbox` folder). Reading starts by itself; progress shows at the bottom. |
| 2 | **Review** | Open the cards that need review, compare each with its photo, fix anything wrong, press **Ctrl+Enter** to approve. |
| 3 | **Export to Anki** | Press **Create the Anki file**, then import that file into Anki (once). |

The **Home** page always tells you the next step.

Use your **original** phone photos, not compressed copies. JPG, PNG, WEBP, BMP and TIFF work; iPhone HEIC photos must be saved as JPG first. Lay the book flat, even light, no finger over the text, whole spread in frame.

### Photo types

Leave **What are they?** on **Detect automatically** (the default). For every photo the app asks the AI one small question: is
it a photo of a two-page book spread, a single page, or a screenshot, and which way must it turn? Then it reads it the right way.
Portrait, landscape and sideways photos are all fine.

| Detected as | How it is read |
|---|---|
| book spread | turned upright, the printed pages are found, each page is read |
| single page / screenshot | turned upright, the whole image is read as one page (no page-finding request) |

You can still choose **book** or **screenshot** yourself; then no detection request is made. If a photo fails (for example a
screenshot added as "book"), go to **Read photos**, set **Photo type for this run** to **detect automatically for all** and press
**Read waiting photos**: failed photos are tried again. A book photo whose usual turn does not fit is also re-turned by itself.

Type an **exam name** when adding photos whose images have no title of their own; it becomes a sub-folder in the inbox (for example
`inbox\BCS 45 Preliminary\page1.png`). A title printed in the image always wins.

The screenshot reader understands answers printed next to a question (`উত্তর: ঘ`, `উ.ক`), one line or table of answers for many
questions, options labelled ক/খ/গ/ঘ or A/B/C/D (cards show them the way the source prints them), unlabelled web-quiz options,
`সমাধান`/`ব্যাখ্যা` solutions with math, figures and title banners. It ignores watermarks, ads, website names and source tags.

---

## 3. The screens

- **Home**: what is waiting, what needs review, what can be exported, today's requests, and the next step.
- **Read photos**: add photos, see the inbox (waiting / read / failed with the reason), start or stop reading.
- **Review**: the list on the left, the photo and the card side by side. Tabs show the question, the whole page, the layout and the
  figure, and **Card look** shows the card as Anki will show it (day and night).
- **Export to Anki**: create the file, copy figures into Anki's media folder automatically (the app finds the folder), see the import steps.
- **Settings**: key, models, how it works, Anki options, subjects and photo types.
- **Database**: backups, rebuilding, AI fill for stored cards, debug bundle, and the "start over" buttons.
- **Help**: a short guide inside the app.

### What the colours mean in Review

- **Must check** (red): the app is not sure; the card waits until you approve it.
- **Note** (yellow): worth a glance; the card is exported anyway.
- No label: exported as it is.
- **Answer by AI**: the source gave no answer and the AI worked it out. An unsure AI answer is a *must check*; a sure one is a note.
  In Anki these cards carry the tag `Answer::AI`, so you can find and re-check them. Every AI answer is a guess to check, especially
  for Bangladesh-specific facts and recent events.
- **AI disagrees with the source**: the cross-check found a different answer, an outdated fact, a wrong explanation or damaged text.
  Choose **AI disagrees with the source** in the list to see only these. Each card shows the AI's verdict with buttons:
  **Use the AI's answer**, **Use the source's answer**, **Use the AI explanation**.

Review buttons: **Approve & next** (Ctrl+Enter) saves your edits and opens the next card; **Save** keeps your edits without approving;
**Skip** moves on; **Reject** throws a card away (nothing is deleted; choose "Rejected" in the list to bring it back).

---

## 4. How the answers and explanations are decided

1. The **answer key** or the **answer printed next to the question** is used when the source has one.
2. If the key and the explanation disagree, or the key could not be read, the card is held for review.
3. If the source has **no answer**, the AI works it out (it actually solves math) and the card is marked as AI-found.
4. If the source has **no explanation**, the AI writes a short, easy one. It is shown on the card only when the source has none,
   and is labelled "AI-generated".
5. **Full cross-check** (Settings → How it works, on by default): after reading and proofreading, every question is sent to the AI
   as **text only** (no image), 8 questions per request. For each question the AI:
   - checks that the question and options make sense. Damaged text is repaired by matching it to the MCQ it must be (these questions
     are usually well known from past exams); if it cannot repair it, the card waits for you as **must check**;
   - **solves the question itself** and compares its answer with the source's answer;
   - checks whether the fact is **outdated** (office holders, counts, rankings, records) and gives the current fact with its year;
   - checks that the explanation is correct, and writes one when the source has none.

   What happens when it disagrees with the source is your choice (**When the AI disagrees with the source**):
   | Choice | Effect |
   |---|---|
   | Hold the card for my review (default) | The source is kept; the card waits in Review with the AI's verdict. |
   | Use the AI's correction when it is sure | High-confidence corrections are applied and labelled on the card ("Answer corrected by AI; the source said …", tag `Answer::AI_corrected`). Anything less sure still waits for you. |
   | Only add a note | The source is kept and the card is exported, with a note. |

   Outdated facts add an **Update (AI)** line to the back of the card and the tag `Check::outdated`. The AI only knows facts up to its
   own training date, so "current" means current as far as the model knows; recent changes can still be missed.
   Cards read before can be checked later: **Database → Cross-check stored cards** (cards checked before are skipped; **Re-check all
   cards** checks them again). Approved cards are never touched.
6. **Proofreading** (Settings → How it works, on by default): after reading, every page's transcription is compared with the page
   picture in one extra request. The AI corrects reading errors only: misread letters and digits (`৪` is four, not 8; `৭` is seven,
   not 9), formulas outside or broken in their LaTeX markers, ditto marks („) in worked solutions written out in full, and text
   attached to the wrong option. It never changes the source's answers, numbers or reasoning; corrections that look like a rewrite
   are refused. Corrected cards get a yellow note, and Review shows the first reading with a **Restore the first reading** button.
   Cards read before can be proofread later: **Database → Proofread stored cards** (approved cards are not touched).
7. **Math** (turn on/off in Settings → How it works):
   - every formula is tested locally for things that stop it rendering (unbalanced braces, `\frac` without arguments, ...), and
     commands written outside the math markers (`\therefore`, `\times`, …) are wrapped automatically, for free, also at export;
   - the AI repairs *formatting only* and says whether the working reaches the answer; it never changes the book's numbers;
   - if a solution is still broken, its picture is read again, focused on the solution;
   - whenever the AI changed an explanation, the **first reading is kept** and shown in Review ("Use this version" restores it).

Optional **Read every page twice with a second model** (Settings → How it works): every page picture is read a second time by a
different model and any difference is flagged. It doubles the reading requests, so it is off by default.

---

## 5. Anki

1. In Anki choose **File → Import** and pick the newest `anki_….txt` file from the `out` folder. Leave "Existing notes" on **Update**.
2. Cards go to decks named `Job Questions::<subject>` using Anki's built-in **Basic** note type. The card design is carried inside
   the cards, so nothing needs to be installed in Anki.
3. If Anki says **"Note skipped, as its notetype was missing"**: your Anki has no note type named exactly `Basic`. Create one
   (Tools → Manage Note Types → Add → Add: Basic), or put your note type's name in Settings → Anki. It needs two fields (Front, Back).
4. **Pictures** (figures): choose Anki's media folder on the Export page and the app copies them for you; otherwise copy the files from
   `out\media` into Anki's `collection.media` folder.
5. **Import each file once.** Importing it again only reports "skipped": the cards are already there. To refresh cards after changing
   something, tick "Include cards exported before" and import again; with "Update" the cards are updated in place, with no duplicates.
6. To start fresh in Anki: delete the `Job Questions` deck in Anki, then export with "Include cards exported before".
7. **Tap to answer** (on by default, Settings → Anki): tap an option and it turns **green** if it is right or **red** if it is
   wrong (the right one is always shown in green). The card turns over by itself to the explanation, and you choose Again, Hard,
   Good or Easy as usual. Pressing Show Answer without tapping just shows the right option in green.
   - Turns over by itself on **Anki for Windows/Mac** and **AnkiDroid**. On the **iPhone** app the colours appear at once and you
     press Show Answer yourself. On AnkiWeb the card works as a normal card.
   - Cards exported before you turned it on (or off) change after you export with "Include cards exported before" and import
     with "Update". Your study progress is kept.

The app only exports **approved** cards and cards with no "must check" items. Cards still waiting for review are held back.

---

## 6. Free quotas, fallback models and key rotation

Free Google keys allow only a few dozen requests **per model per day** (the exact numbers change; look at https://ai.dev/rate-limit).
| Step | Requests |
|---|---|
| Detect the photo type | 1 per photo (none if you choose the type yourself) |
| Find the pages | 1 per book spread |
| Read | 1 per page |
| Proofread against the photo | 1 per page |
| Full cross-check | 1 per 8 questions |
| Math check | 1 per 6 math explanations, plus a picture re-read for what is still broken |

A book spread with about 25 questions therefore takes about 10 requests; a screenshot with 10 questions about 4.

- **Fallback models** (Settings → Models) are tried automatically when a model is out of quota or busy. Each model has its own allowance.
- When everything is used up, the app **stops cleanly** and tells you when to continue (midnight Pacific time). Finished work is saved,
  and answers already received are never requested twice.
- The Home page shows how many requests you used today per model.
- "High demand" errors are Google being busy, not your quota; the app retries and falls back by itself.

### One key or key rotation

**Settings → Google AI key → Which key to use** switches between:

- **Use one key**: the key saved above it. Use this for a paid key (and raise *Requests per minute* and *Parallel requests* in
  *How it works*), or for a single free key. The key list is kept but not used.
- **Rotate through the key list**: every request goes to the key that has been used least today for that model, counting requests
  that are still running, so parallel requests spread evenly. The key list plus the key above are used.

**Test every key** works in both modes, so you can check the list before switching.

### Key rotation (at your own risk)

**Read this first:** Google's terms do not allow using several projects/accounts to get around a quota, and its developer forum has
reports of projects being restricted for it. Nothing in this app can make that safe. Only use keys and accounts you are willing to lose,
never your main account. The app protects what it can: each key has a daily cap, invalid keys are retired, and **everything stops if
Google blocks two keys** (until you press "Clear invalid / blocked marks"). Keys are never written to the log, the database or the
debug bundle; only the Settings page shows the key list, so you can edit it.

---

## 7. Settings reference

| Setting | Meaning |
|---|---|
| Models: pages / reader / second reader / AI | Which Gemini model does what. The second reader is only used for the cross-check. |
| Fallback models | Tried, in order, when a model is out of quota or busy. |
| AI proofreads every page | Compares each reading with the photo and corrects reading errors. 1 request per page. |
| Which key to use | One key, or rotation through the key list. |
| AI cross-checks every question | Text-only check of question, options, answer, up-to-dateness and explanation. 1 request per 8 questions. |
| When the AI disagrees with the source | Hold for review / use sure corrections / note only. |
| AI fills gaps | Missing answers and explanations (labelled); only used when the cross-check is off. |
| Math check and repair | Formula test, AI repair, picture re-read. |
| Read every page twice with a second model | Second reading of each page picture. Doubles reading requests. |
| Hold back cards that only have notes | Normally only "must check" cards wait. |
| Requests per minute / parallel requests | Pacing. Set the first to your real limit. |
| Daily cap per key / stop if keys blocked | Only for key rotation. |
| Deck name / note type / grey line | Anki options. The grey line shows the exam, optionally the page and question number. |
| Subjects | The subjects the AI may assign; they become deck names. |
| Photo types | For "book" and "screenshot" when you choose them yourself: degrees to turn upright (0, 90, 180, 270 or auto). |
| Automatic backup | Database page: back up before every task, and how many automatic backups to keep. |

Changes are saved in `settings.json` and apply from the next task. "Reset all settings" deletes that file (your keys are not touched).
`config.toml` holds the defaults; you can edit it by hand, but `settings.json` always wins.

---

## 8. Database, backups and starting over

Everything you approve lives in `data\qb.sqlite`. **Automatic backup** (on by default, Database page) copies it into `data\backups`
before every task that changes cards: reading, rebuilding, AI fill and proofreading. The newest 10 automatic backups are kept
(you can change the number); backups you make yourself with **Back up now** are never deleted. Restoring a backup first saves the
current database under its own name.

| Button | Effect |
|---|---|
| Rebuild from saved answers | Re-applies the current rules to photos already read, using answers saved earlier. Steps that did not exist then (such as proofreading) make new requests. Photos with approved or exported cards are skipped. |
| Cross-check stored cards / Re-check all cards | Full AI cross-check of cards read before (1 request per 8 cards). Cards whose answer or text changed and that were exported can be exported again. |
| Proofread stored cards | Proofreads cards read before against their page pictures (1 request per page). Corrected cards that were exported can be exported again; import with "Update" to refresh them in Anki. |
| Make a debug bundle | A zip with the database, log, settings and crops, for finding a problem. Never contains keys. |
| Allow re-exporting every card | Clears the "exported" marks. |
| Forget all cards | Empties the database (a backup is kept). Saved AI answers stay, so reading the photos again costs nothing. |
| Delete everything incl. saved AI answers | Also removes crops and saved answers: every photo goes to the AI again. |

Your photos in `inbox`, your settings and your keys are never touched by these.

### Moving the project to Google Drive or another PC

- **Copy**: `data` (your cards and backups), `work` (crops and saved AI answers; large, but it saves requests), `settings.json`, `config.toml`,
  `inbox`, `out`, and the program files. `keys.txt` and `.env` hold your keys: keep them private, do not share the folder.
- **Do not copy `.venv`**: it only works on the computer that made it. On a new PC unzip/copy the folder, run **setup.bat** once, then
  **start.bat**. Moving the folder is safe: the app stores its paths relative to its own folder.
- **Run the app from a normal local folder**, and use Drive as a copy/backup. Do not run it from inside Google Drive's virtual drive
  ("stream files" mode, `G:\`): the database does not work reliably on a virtual or network drive. If you use Drive's "mirror files"
  mode the folder is local and that is fine, but close the app before Drive syncs it.

---

## 9. If something goes wrong

| What you see | What to do |
|---|---|
| The app is already open (double-clicked start.bat twice) | The second window says so and just opens the browser. |
| `setup.bat`: "Python 3.12 was not found" | Install Python 3.12 (64-bit) from python.org, then run setup.bat again. |
| Errors about `_imaging`, `pydantic_core` or "package BROKEN" | Python is probably too new (3.14+) or an install was interrupted. Install Python 3.12, delete the `.venv` folder, run setup.bat again. Also check that antivirus did not quarantine a file in `.venv`. |
| "No API key found" / Home asks for a key | Settings → Google AI key. |
| A model "not found" / "no longer available" | Settings → Models → Find working models → Use these. |
| "NO quota for this model (limit 0)" | That model is not available on free keys (typical for Pro models). Use a Flash model. |
| "Daily quota used up" | Wait until the time shown (midnight Pacific), then read again. Nothing is lost. |
| Many "503 / high demand" lines | Google is busy. The app retries and uses fallback models; run again later if a photo failed. |
| A photo "failed" | Read photos → Photo type for this run: **detect automatically for all** → Read waiting photos. If it still fails, retake it flatter with the whole spread in frame. |
| A proofreading correction is wrong | Review → **Restore the first reading**, then Save or Approve. |
| Anki: "notetype was missing" | See section 5, point 3. |
| Anki: everything "skipped" | You imported the same file twice; the cards are already there. |
| Something else | Database → Make a debug bundle and send the zip (it contains no keys). |

The black window shows the app's log; `data\qb.log` keeps a copy.

---

## 10. Command line (optional)

`python -m qb2anki` (or start.bat) opens the app. Everything also works from the command line, using `.venv\Scripts\python.exe -m qb2anki <command>`:
`run [--type auto|book|screenshot]`, `export [--all] [--anki-media PATH]`, `status`, `rebuild`, `fill`, `proofread`, `check [--all]`,
`keys [--check] [--reset]`, `doctor`, `models [--test]`, `bundle`.

## 11. What is in this folder

| Item | Purpose |
|---|---|
| `start.bat`, `setup.bat` | Open the app; one-time installation. |
| `config.toml`, `settings.json` | Defaults you may edit; what the Settings page saves. |
| `.env`, `keys.txt` | Your API key and the optional key list (private). |
| `inbox`, `work`, `data`, `out` | Photos to read; crops and saved AI answers; database, backups and log; Anki files. |
| `qb2anki` | The program. |
| `tests` | Automated tests for developers (`python -m unittest discover -s tests`); not needed to use the app. |
