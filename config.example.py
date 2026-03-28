# Email configuration
# Rename this file from config.example.py to config.py
# Fill these in before running mailer.py
# See README.md for Gmail setup instructions.

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

SMTP_USER = "your.email@gmail.com"     # your Gmail address
SMTP_PASS = "xxxx xxxx xxxx xxxx"      # your Gmail App Password (not your real password)

EMAIL_FROM = "your.email@gmail.com"    # same as SMTP_USER usually
EMAIL_TO   = "your.email@gmail.com"    # where to send alerts (can be same or different)
