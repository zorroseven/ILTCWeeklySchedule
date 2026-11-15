# ILTC Weekly Schedule

Copies the week's chatter schedule from the ILTC scheduling site into
Infloww's **Employees > Shift schedule**.

For each account you pick it:

1. opens the account's **View week schedule** popup on the site (in Chrome)
   and reads who is on Grave, Afternoon and Main each day;
2. filters Infloww's shift calendar to that account's creator and pages to
   the same week;
3. creates whatever is missing, one weekly-repeating shift per person per
   slot (ticking the days they work, ending on the Saturday);
4. deletes every shift that is not on the site - wrong person, or right
   person at the wrong time - choosing "This and all future shifts";
5. reads the week back and reports anything that still doesn't match.

It reads both pages' text through Windows UI Automation, so names come
through exactly - no OCR. While it runs it drives the real mouse and
keyboard, so leave the PC alone until it finishes.

## Using it

1. In Chrome, open the scheduling page and log in. Have Infloww open.
2. Open `dist\ILTC WEEKLY SCHEDULE\ILTC WEEKLY SCHEDULE.exe` (or `launch.vbs` from source).
3. **Load from site** the first time, then click the accounts to do
   (shift-click for a range). The box next to each name is its Infloww
   creator code (`ANT` for "ANT - 🍉 Antje"); fill in any that are blank.
4. **Preview** shows exactly what would be created and deleted without
   touching anything. **Run** does it.

Shift times default to the Norway clock numbers the team already uses in
Infloww: Grave 02:00-10:00, Afternoon 10:00-18:00, Main 18:00-02:00. They
can be changed in the app.

Creating happens before deleting. If a shift can't be created (say an
employee name on the site isn't in Infloww), nothing is deleted for that
account, so a slot is never left empty by mistake.

## Files

| File | Role |
| --- | --- |
| `ILTC_WEEKLY_SCHEDULE.py` + `gui.html` | the window |
| `schedule_worker.py` | separate process that does the automation |
| `site_core.py` | reads the scheduling site out of Chrome |
| `infloww_core.py` | drives Infloww's Shift schedule |
| `schedule_shared.py` | config, creator codes, and the create/delete plan |
| `schedule_config.json` | your codes, times and selection |
| `schedule_gui.log` | diagnostics |

## Setting up from source

```
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Building the .exe

```
powershell -ExecutionPolicy Bypass -File build.ps1
```

Produces `dist\ILTC WEEKLY SCHEDULE\ILTC WEEKLY SCHEDULE.exe`. Keep the
`_internal\` and `worker\` folders next to it - the app runs the worker as
a separate process.
