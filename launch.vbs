' Double-click to open ILTC WEEKLY SCHEDULE without a console window.
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
CreateObject("WScript.Shell").Run """" & here & "\.venv\Scripts\pythonw.exe"" """ & here & "\ILTC_WEEKLY_SCHEDULE.py""", 0, False
