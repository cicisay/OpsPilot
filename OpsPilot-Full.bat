@echo off
rem OpsPilot console - full mode quick launcher (ASCII only on purpose)
rem Double-click = START-WINDOWS.bat full (approval + publish enabled)
rem
rem %~dp0 is this file's own folder. The launcher used to hard-code an absolute
rem path to a different checkout, so double-clicking it ran that copy's code and
rem silently ignored every edit made here.
call "%~dp0START-WINDOWS.bat" full
