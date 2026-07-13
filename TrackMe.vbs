' Launch TrackMe with NO command-prompt window.
' Uses "pyw" (the windowless Python launcher) run hidden via WScript, so only the
' Tkinter window appears. Double-click this instead of TrackMe.bat.
' Any arguments (log folder / character name) are passed straight through.
Option Explicit
Dim shell, fso, here, cmd, i, args
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = here
cmd = "pyw """ & here & "\trackme.py"""
For i = 0 To WScript.Arguments.Count - 1
    cmd = cmd & " """ & WScript.Arguments(i) & """"
Next
' 0 = hidden window, False = don't wait for it to exit.
shell.Run cmd, 0, False
