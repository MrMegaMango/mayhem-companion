Option Explicit
Dim shell, fs, root, python, appPath, shortcutPath, arguments, shortcut, disable
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
If WScript.Arguments.Count < 1 Then WScript.Quit 2
root = fs.GetAbsolutePathName(WScript.Arguments(0))
python = root & "\.venv\Scripts\pythonw.exe"
appPath = root & "\app"
shortcutPath = shell.SpecialFolders("Startup") & "\Mayhem Companion - Start with League.lnk"
arguments = "-m mayhem.autostart"
disable = False
If WScript.Arguments.Count > 1 Then disable = (LCase(WScript.Arguments(1)) = "disable")
If fs.FileExists(shortcutPath) Then
    Set shortcut = shell.CreateShortcut(shortcutPath)
    If LCase(shortcut.TargetPath) <> LCase(python) Or shortcut.Arguments <> arguments Then
        WScript.Echo "A different application owns this startup shortcut; it was not changed."
        WScript.Quit 3
    End If
End If
If disable Then
    If fs.FileExists(shortcutPath) Then fs.DeleteFile shortcutPath
    WScript.Echo "Start with League disabled. The launch watcher will stop shortly."
    WScript.Quit 0
End If
If Not fs.FileExists(python) Or Not fs.FileExists(appPath & "\mayhem\autostart.py") Then
    WScript.Echo "Install Mayhem Companion before enabling automatic launch."
    WScript.Quit 4
End If
Set shortcut = shell.CreateShortcut(shortcutPath)
shortcut.TargetPath = python
shortcut.Arguments = arguments
shortcut.WorkingDirectory = appPath
shortcut.Description = "Start Mayhem Companion when the League client opens"
shortcut.IconLocation = python & ",0"
shortcut.WindowStyle = 7
shortcut.Save
shell.CurrentDirectory = appPath
shell.Run Chr(34) & python & Chr(34) & " " & arguments, 0, False
WScript.Echo "Start with League enabled for this Windows user."
