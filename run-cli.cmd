@echo off
rem Run the command line interface: run-cli.cmd extract --mode 3 --out out book.pdf
"%~dp0.venv\Scripts\python.exe" -m pdfextract %*
