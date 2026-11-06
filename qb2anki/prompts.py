"""Prompts. The prompt text itself is part of the cache key, so editing a prompt invalidates its cached answers automatically."""
from __future__ import annotations

import json

PROMPT_VERSION = "v1"

SYSTEM = (
    "You are a meticulous OCR and document-layout engine for printed Bangladeshi job-exam question banks "
    "(Bangla and English mixed). You transcribe exactly what is printed. You never translate, correct, "
    "summarise, or invent text. Output must be valid JSON that matches the requested schema."
)

ORIENTATION = """This is a photo of a printed book page or spread, possibly rotated sideways or upside down.
How many degrees COUNTER-CLOCKWISE must the image be rotated so that the printed text reads normally
(left to right, top to bottom)? Answer 0, 90, 180 or 270."""

LAYOUT = """This is a photo of a printed MCQ book showing one or two book pages (a spread).
Locate each printed page. Coordinates are [ymin, xmin, ymax, xmax], integers from 0 to 1000,
normalised to THIS image (0,0 is the top-left corner; y is vertical, x is horizontal).

Return, for each printed page visible, in left-to-right order:
- printed_page: the page number printed in the page's header/footer strip (e.g. "568/Professor's JOB SOLUTION" -> 568,
  "Professor's JOB SOLUTION/569" -> 569). Use Western digits. null if you cannot read it.
- page_box: the whole page: its header strip and everything printed on it (questions, answer table, title block), but not
  the table, hands or background around it.

Also set text_upright=true only if the text in this image already reads normally (not rotated, not upside down)."""

def page_prompt(subjects: dict) -> str:
    subj = "\n".join(f"- {name}: {desc}" for name, desc in subjects.items())
    return f"""This is a crop of ONE printed page of a Bangla/English MCQ book (it may include the thin page header strip).
The page has two text columns separated by a dotted vertical line. Read the LEFT column from top to bottom, then the RIGHT
column from top to bottom, and transcribe every question.

For each question return:
- number: the question number as printed.
- stem: the question text, exactly as printed (keep the original script; do not translate; do not fix spelling).
- options: the answer options in printed order. 'label' is the letter in the circle (ক, খ, গ or ঘ); 'text' is the option text.
  Options may be laid out in a grid; always assign each text to the label printed next to it.
- explanation: the text of the explanation paragraph. In the book it starts after a circled word 'ব্যাখ্যা'; do NOT include
  that word. Keep line breaks as \\n. Use "" if the question has no explanation.
- answer_in_explanation: if the explanation or a note EXPLICITLY states which option is correct (for example
  "Option (খ) সঠিক" or "(ক) উত্তর"), give that option's label; otherwise null. Never work out the answer yourself here.
- subject: one of the subjects below.
- has_figure / figure_box: true and the box of a diagram or geometry figure that belongs to this question or its explanation.
  Otherwise false and null.
- question_box: the box of the whole question (stem, options and explanation).
- truncated_top / truncated_bottom: true if the question is cut off by the top / bottom edge of this crop.

Also return:
- answer_key: every row of the small boxed answer table (it has a round logo and the word 'উত্তর', then rows such as '৩৭ গ'),
  usually in a lower outer corner of the page. For each row give 'number' with the digits as printed and 'answer' exactly as
  printed (ক, খ, গ, ঘ or Note; use '?' if a cell is unreadable). This table is NOT a question. Empty list if the page has none.
- exam_header: if a large title block announcing the start of a new exam (exam or post title, date, time, marks) appears on
  this page, transcribe it (title, exam_date, duration, total_marks, other_text); otherwise null. It is NOT a question.

Mathematics: write mathematical expressions in LaTeX, inline as \\( ... \\) and displayed as \\[ ... \\]. Use ASCII digits 0-9
inside LaTeX. Use \\frac{{a}}{{b}} for fractions and \\sqrt{{ }} for roots. Keep Bangla words outside the math delimiters. Put each
step of a worked solution on its own line (separate lines with \\n) and write the therefore sign as \\therefore.
Outside LaTeX, keep digits exactly as printed (Bengali digits stay Bengali).

Strict rules:
- Ignore page headers/footers, dotted separator lines and stray marks.
- Ignore faint ghost text that bleeds through from the reverse side of the paper.
- Do not skip any question and do not merge two questions. Questions in the right column come after those in the left column.
- If a character is genuinely unreadable write [?] in its place; never guess.
- Boxes are [ymin, xmin, ymax, xmax], integers 0-1000, normalised to THIS crop.

Subjects (use these names exactly; use "Other" only if none fits):
{subj}"""


def general_page_prompt(subjects: dict) -> str:
    """For anything that is not a photo of this particular book: PDF pages, web screenshots, newspaper images..."""
    subj = "\n".join(f"- {name}: {desc}" for name, desc in subjects.items())
    return f"""This is an image of multiple-choice questions (MCQs), mostly Bangla and English. It may be a screenshot of a web quiz,
a page of a PDF, a scanned or photographed page, or a newspaper/blog picture. It can have one to four text columns.
Transcribe EVERY question, in reading order (columns left to right, each column top to bottom).

For each question return:
- number: the question number as printed ('' if the question has no number).
- stem: the question text, exactly as printed (keep the original script; do not translate; do not fix spelling).
- options: the answer options in printed order. 'label' is the label printed for the option, whatever style it uses:
  ক/খ/গ/ঘ (possibly circled or followed by '.' or ')'), A/B/C/D, a/b/c/d. If the options have NO printed label (for example
  radio buttons on a web page), label them ক, খ, গ, ঘ in order for Bangla questions, or A, B, C, D for English questions.
  Options may be laid out in a grid; always assign each text to its own label.
- explanation: the explanation or worked solution if the source gives one (it may be introduced by 'ব্যাখ্যা', 'সমাধান',
  'Solution', 'Explanation'; do NOT include that introducing word). Keep line breaks as \\n. Use "" if there is none.
- answer_in_explanation: the label of the correct option IF THE SOURCE ITSELF GIVES IT for this question: printed next to the
  question (for example 'উত্তর: ঘ', 'উ. ক', 'উ.খ', 'Ans: C'), or stated in the explanation, or visibly marked as correct
  (tick, highlighted/green option, bold). Otherwise null. Never work the answer out yourself.
- subject: one of the subjects below.
- has_figure / figure_box: true and the box of a diagram or geometry figure that belongs to this question or its solution.
  Otherwise false and null.
- question_box: the box of the whole question (stem, options, answer and explanation).
- truncated_top / truncated_bottom: true if the question or its solution is cut off by the top / bottom edge of the image.

Also return:
- answer_key: if the image has a table or a line listing the answers for several questions (for example 'উত্তর: ৩১.A ৩২.B
  ৩৩.C ...' or a small boxed table), give every pair: 'number' with the digits as printed and 'answer' exactly as printed
  (ক, খ, গ, ঘ, A, B, C, D, or Note; '?' if unreadable). Empty list if there is none. Answers printed right next to a single
  question belong in that question's answer_in_explanation, NOT here.
- exam_header: if the image has a title or banner that names the exam or source (for example 'বিসিএস প্রিলিমিনারি পরীক্ষা',
  '৫০তম বিসিএস প্রিলিমিনারি, তারিখ: ৩০.০১.২০২৬'), transcribe it (title, exam_date, duration, total_marks, other_text);
  otherwise null. A section heading such as 'বাংলাদেশ' or 'English' is NOT an exam title and NOT a question.

Mathematics: write mathematical expressions in LaTeX, inline as \\( ... \\) and displayed as \\[ ... \\]. Use ASCII digits 0-9
inside LaTeX. Use \\frac{{a}}{{b}} for fractions and \\sqrt{{ }} for roots. Keep Bangla words outside the math delimiters. Put each
step of a worked solution on its own line (separate lines with \\n) and write the therefore sign as \\therefore.
Outside LaTeX, keep digits exactly as printed (Bengali digits stay Bengali).

Strict rules:
- Ignore: watermarks, website names and logos, advertisements, pictures of books, page numbers, page headers/footers, phone numbers,
  author lines, dotted separators and stray marks.
- Ignore small source tags and references attached to questions, such as '[৫০তম বিসিএস প্রিলিমিনারি -২৬]' or
  'পৃষ্ঠা: ৮৪ (অগ্রদূত বাংলাদেশ বিষয়াবলি, ৫ম সংস্করণ)'. They are not part of the question or the explanation.
- Do not skip any question and do not merge two questions.
- If a character is genuinely unreadable write [?] in its place; never guess.
- Boxes are [ymin, xmin, ymax, xmax], integers 0-1000, normalised to THIS image.

Subjects (use these names exactly; use "Other" only if none fits):
{subj}"""


FILL_SYSTEM = (
    "You are a careful exam tutor for Bangladeshi job exams (BCS, bank, government). You answer multiple-choice questions "
    "correctly and write short, plain explanations a student can learn from. You never invent facts."
)


def fill_prompt(payload: list[dict]) -> str:
    return f"""Each item below is an MCQ taken from a source. For each item do what its flags ask:
- need_answer = true: the source does not give the answer. Work it out and give the label of the correct option in 'answer'
  (exactly one of the option labels shown). For maths and reasoning, actually solve it step by step before answering. If you cannot
  decide, set answer to null. Set 'confidence' to high only when you are sure, medium when likely, low when unsure.
- need_explanation = true: write a short, easy explanation (2-4 sentences, plain words) of why the correct option is correct.
  Write it in the language of the question (Bangla for Bangla questions, English for English questions). For maths show the key
  working in LaTeX, inline as \\( ... \\); use ASCII digits inside LaTeX. State only facts you are sure of; never invent dates,
  names or statistics.
- If an item already has a 'key' (the source's answer), the explanation must support that key. If you believe the key is wrong,
  still explain the key but set confidence to low.
- Leave 'explanation' empty if need_explanation is false. 'id' must be copied exactly.

Items (JSON):
{json.dumps(payload, ensure_ascii=False, indent=1)}"""


MATH_SYSTEM = (
    "You are a careful mathematics editor. You check LaTeX transcriptions of worked solutions from printed books. "
    "You repair formatting only and you never change the book's mathematics."
)


def math_check_prompt(payload: list[dict]) -> str:
    return f"""Each item below is a worked solution that was transcribed by OCR from a printed Bangladeshi exam book. You cannot see the
book, only the transcribed text. Mathematics is written in LaTeX: inline \\( ... \\), displayed \\[ ... \\]; lines are separated by \\n.
For each item:
1. FORMULAS. Check that every formula would render: balanced braces; \\frac{{a}}{{b}} and \\sqrt{{...}} with all their arguments; matching
   \\left/\\right and \\begin/\\end; no Bangla words inside a formula (Bangla text belongs OUTSIDE the math delimiters); the
   therefore sign written \\therefore; each step of the working on its own line. If something must change, return the whole corrected
   text in 'fixed_explanation'. Make ONLY formatting/syntax repairs, and slips that are certain from the surrounding working. Never
   change the book's numbers, reasoning or final answer. If nothing needs changing, return "" for 'fixed_explanation'.
2. CONSISTENCY. 'consistent' is true when each step follows from the previous one and the working ends at the option given as 'key'
   (if there is a key). It is false when a step does not follow (for example wrong arithmetic) or the result differs from the key. In
   that case say what is wrong in one short sentence in 'note' and do NOT repair the mathematics yourself.
'id' must be copied exactly.

Items (JSON):
{json.dumps(payload, ensure_ascii=False, indent=1)}"""


def math_reread_prompt(problems: list[str]) -> str:
    probs = "; ".join(problems) if problems else "its formulas did not render"
    return f"""This image shows ONE multiple-choice question from a printed book, with its explanation or worked solution.
Transcribe ONLY the explanation / worked solution again, as carefully as you can (it starts after 'ব্যাখ্যা' or 'সমাধান'; do not
include that word, the question, or the options). A previous transcription had these problems: {probs}.
Write mathematics in LaTeX, inline as \\( ... \\) and displayed as \\[ ... \\]; use ASCII digits 0-9 inside LaTeX; \\frac{{a}}{{b}} for
fractions, \\sqrt{{ }} for roots, \\therefore for the therefore sign. Keep Bangla words outside the math delimiters. Put each step on
its own line (separate lines with \\n). Keep digits outside LaTeX exactly as printed. If a character is unreadable write [?]."""


DETECT = """Look at this image of multiple-choice questions and answer two things.
1. kind:
   - "spread": a PHOTO of an open book or booklet showing TWO printed pages side by side (with the spine or gutter between them).
   - "page": a photo or scan of ONE printed page, or part of one.
   - "screenshot": a digital image: a screenshot of a website, app, PDF viewer or document, or a digital picture such as a
     newspaper clipping or a social-media image.
2. rotate_ccw_degrees: how many degrees COUNTER-CLOCKWISE the image must be rotated so that its text reads normally
   (left to right, top to bottom): 0, 90, 180 or 270. Phone photos of an open book are often sideways."""

PROOF_SYSTEM = (
    "You are a meticulous proofreader of OCR transcriptions of printed Bangladeshi job-exam question banks (Bangla and English). "
    "You compare a transcription with the page image and correct reading errors only. You never rewrite, translate or improve "
    "the source, and never change its answers, numbers or reasoning."
)


def proofread_prompt(payload: list[dict]) -> str:
    return f"""The image is ONE page of a multiple-choice question source. Below is a machine transcription of its questions (JSON).
Compare every item carefully with the image and correct TRANSCRIPTION ERRORS only:
- wrong, missing or extra letters and words, especially Bengali conjuncts and vowel signs;
- wrong digits. Bengali digits: ০=0 ১=1 ২=2 ৩=3 ৪=4 ৫=5 ৬=6 ৭=7 ৮=8 ৯=9. Be careful: ৪ is FOUR (it looks like the Latin 8), ৮ is EIGHT,
  ৭ is SEVEN (it looks like 9), ৯ is NINE, ০ is ZERO. Inside LaTeX write the ASCII digit with the SAME value (৪ -> 4, never 8).
  Outside LaTeX keep digits exactly as printed;
- mathematics: every mathematical expression must be LaTeX inside \\( ... \\) or \\[ ... \\], including \\therefore, \\because, \\times,
  \\Rightarrow, fractions (\\frac{{a}}{{b}}) and roots (\\sqrt{{ }}). Repair broken LaTeX. Bangla words go OUTSIDE the delimiters.
  Put each step of a worked solution on its own line (separate lines with \\n);
- ditto marks: worked solutions often write „ or " " under words of the line above to mean "the same as above". Replace every
  ditto mark with the word(s) it stands for, so each line reads in full;
- option texts attached to the wrong label, text that belongs to another question or to an answer table, explanation text
  that is missing or cut short although it is printed.
Do NOT translate, rephrase, fix the source's own grammar or spelling, change its numbers, working or answers, or add anything
that is not printed on the page.

Return ONLY the items that need a correction, each with ALL its fields in corrected form: stem, options (the same labels, in the
same order) and explanation, plus 'changes': one short note of what you corrected. Return an empty list if everything is correct.
'id' must be copied exactly.

Items (JSON):
{json.dumps(payload, ensure_ascii=False, indent=1)}"""


CHECK_SYSTEM = (
    "You are an expert examiner and fact-checker for Bangladeshi job exams (BCS, bank, government recruitment): Bangla, English, "
    "mathematics, general knowledge, science and ICT. You verify multiple-choice questions transcribed from books and websites. "
    "You are careful and honest: you never invent facts, and you say when you are unsure."
)


def check_prompt(payload: list[dict]) -> str:
    return f"""Each item below is a multiple-choice question transcribed by OCR from an exam book, a PDF or a website. Check EVERYTHING in it,
using your own knowledge (there is no image). For each item:

1. TEXT. Does the question and do the options read as a sensible, well-formed MCQ? OCR damage looks like nonsense words, broken or
   wrong numbers, options that belong to a different question, or a question cut in half. Ordinary spelling variants, old spellings
   and the source's own style are NOT damage. If the text is damaged and you recognise the MCQ it must be (these questions are
   often well known from past exams), set text_ok=false and give the repaired text in fixed_stem and fixed_options (the same labels,
   in the same order). Repair only what is damaged, keep the wording otherwise. If it is damaged and you cannot repair it with
   confidence, set text_ok=false and leave the fixed fields empty. If the text is fine, text_ok=true and leave them empty.
2. ANSWER. Decide the correct option yourself, independently of 'source_answer' ('answer' = one of the option labels shown, or null if
   none is correct or you cannot tell). For mathematics and reasoning actually solve it step by step. 'answer_confidence': high only
   when you are sure, medium when likely, low when unsure. 'agrees': "yes" if your answer equals source_answer, "no" if it differs,
   "no_source" if source_answer is null.
3. UP TO DATE. Facts that change over time (office holders, counts, rankings, records, latest events, prices, laws, census figures):
   if the answer was right when the exam was written but is no longer true, set outdated=true and in outdated_note give the current
   fact as far as you know it, with the year. Otherwise outdated=false.
4. EXPLANATION. If 'explanation' is not empty, is it correct and does it support the correct answer? explanation_ok=true/false; if
   false, say what is wrong in explanation_issue. If need_explanation is true, or the explanation is wrong, write a short, correct,
   easy explanation (2-4 sentences) in 'explanation', in the language of the question (Bangla for Bangla questions, English for English
   ones). For mathematics show the key working in LaTeX inside \\( ... \\) with ASCII digits. Otherwise leave 'explanation' empty.
5. 'note': one short sentence on anything else a student should know, or "".

Never invent dates, names or statistics. 'id' must be copied exactly. Keep Bengali digits outside LaTeX as written.

Items (JSON):
{json.dumps(payload, ensure_ascii=False, indent=1)}"""
