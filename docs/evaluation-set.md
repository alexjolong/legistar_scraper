# The evaluation set: what is needed and how to write it

This is a request for 30 real questions with their right answers, written by people who
know the county's record. It is the one thing the tool cannot produce for itself, and
several decisions are waiting on it.

## What it is for

Every choice so far about search and answers was checked against questions made up by
the tool's author, which shows only that it works on questions he thought of. With
questions from the people who would use it, each of these becomes a measurement
instead of a guess:

- which embedding model to use (the local one, or one of the two others available in
  Databricks);
- whether a question should lead with the record or with the law;
- which chat model writes answers, and how often it misstates a fact;
- whether a later change made things better or worse. The set is run again after
  every change.

## What one entry looks like

One row per question in [eval/questions.csv](../eval/questions.csv). It opens in Excel.
Where a cell holds several values, separate them with semicolons.

| Column | What goes in it | Required |
|---|---|---|
| `id` | Any short label: `q01`, `q02` | yes |
| `question` | The question exactly as a person would type it. Misspellings, acronyms and vague wording are welcome if that is how people ask | yes |
| `asker` | `topical` (someone in a program who does not know file numbers) or `tracker` (someone following a specific item) | yes |
| `kind` | One of the kinds listed under [Breadth](#breadth) | yes |
| `must_find` | File numbers that a good search must return, fewest possible: `26-0333; 26-0333 R1`. For a law question, the statute section or ordinance: `163.051`, `Ordinance 13` | yes, unless `no_answer` |
| `also_fine` | Other items that are reasonable to return and should not count against the result | no |
| `answer_facts` | Two to four short facts a correct answer has to contain: an action and its date, a body, an amount, a status. `adopted 2026-03-17 by the Board; referred first to Public Works` | yes, unless `no_answer` |
| `must_not_say` | A tempting wrong answer, if there is one: `that 26-0333 itself was adopted (its revision was)` | no |
| `no_answer` | `yes` when the record does not hold the answer, and the right response is to say so | only then |
| `as_of` | The date on which you checked the answer. The record moves; this says when the row was true | yes |
| `how_known` | Where the answer comes from, if not obvious: `minutes of the 3/17 board meeting` | no |

Three things make an entry useful:

1. **Write the question before looking anything up.** A question written after reading
   the item borrows the item's words, and then keyword search looks better than it is.
2. **Keep `must_find` minimal.** List what would make you say "it missed it" if absent,
   not everything related.
3. **Use dates and numbers in `answer_facts`.** "Approved in the spring" cannot be checked;
   "approved 2025-05-06" can.

## Breadth

Thirty questions, spread roughly like this. Exact counts matter less than having every
kind present, including the ones the tool is expected to find hard.

| `kind` | How many | What it tests | Example of the shape |
|---|---|---|---|
| `lookup` | 3 | A file number is given; where does the item stand | "Where is 26-0333?" |
| `named-item` | 3 | A specific item, but named by vendor, agreement number or project, not file number | "the contract with ⟨vendor⟩ for ⟨service⟩" |
| `topic` | 5 | What the board has done in an area; several right items | "embedded social workers in police departments" |
| `revision` | 2 | The item was revised or substituted (`R1`, `S1`) and the answer depends on which one | "was ⟨item⟩ approved as proposed?" |
| `path` | 2 | Which bodies heard it, in what order, and how the vote went | "did ⟨item⟩ go through committee?" |
| `bounded` | 2 | A limit by time, department or body | "housing items since January 2025" |
| `amount` | 2 | A dollar amount, term or date from the resolution | "how much was authorized for ⟨project⟩?" |
| `vocabulary` | 2 | The asker's words differ from the record's: acronyms, program nicknames, plain words for formal ones | "ERF grants", "the garbage burner" |
| `law` | 5 | What a statute or ordinance says: authority, a requirement, what a session changed | "what law lets the county charge a wheelage tax?" |
| `law-and-record` | 2 | Both together | "what has the board done under ⟨statute⟩?" |
| `no-answer` | 2 | The record cannot answer: what was said in discussion, something that never came to the board, the terms of the signed contract | "why did Commissioner ⟨name⟩ object?" |

Across the thirty, please also spread:

- **Years**: some from each of 2023, 2024, 2025 and 2026.
- **Bodies**: mostly the county board and its committees, with two or three from the
  Regional Railroad Authority and the Housing and Redevelopment Authority.
- **Departments**: at least five different requesting departments, and two or three
  items that have no requesting department recorded.
- **Difficulty**: about a third that you expect the tool to get easily, a third you are
  unsure of, and a third you expect it to miss.

One kind is deliberately absent: "list everything waiting on ⟨committee⟩". The Coming up
page answers that from the graph exactly, and a model should not be asked to.

## Practical notes

- Fifteen minutes an entry is about right. Questions that colleagues have really asked
  are better than invented ones; a few contributors writing ten each would be ideal.
- Use only what is in the public record. No names of people who asked, and nothing from
  internal correspondence.
- An entry whose answer later changes is still useful: update `answer_facts` and `as_of`.
- Imperfect is fine. Twenty good entries now are worth more than thirty later; the kinds
  can be filled in over time.
