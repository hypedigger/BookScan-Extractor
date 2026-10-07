@echo off
rem Launch the graphical interface through the console-free launcher created by
rem install.ps1. Falls back on the windowed entry point if it is not there yet.
if exist "%~dp0.venv\Scripts\pdfextract-launcher.exe" (
  start "" "%~dp0.venv\Scripts\pdfextract-launcher.exe" -m pdfextract %*
) else (
  start "" "%~dp0.venv\Scripts\pdfextract-gui.exe" %*
)
