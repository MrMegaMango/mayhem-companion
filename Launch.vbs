Option Explicit
Dim shell, fs, root, python, command
Set shell = CreateObject("WScript.Shell")
Set fs = CreateObject("Scripting.FileSystemObject")
root = fs.GetParentFolderName(WScript.ScriptFullName)
python = root & "\.venv\Scripts\pythonw.exe"
If Not fs.FileExists(python) Then
    MsgBox "Run Setup.ps1 before opening Mayhem Companion.", 48, "Mayhem Companion"
    WScript.Quit 1
End If
shell.CurrentDirectory = root & "\app"
command = Chr(34) & python & Chr(34) & " -m mayhem"
shell.Run command, 0, False
