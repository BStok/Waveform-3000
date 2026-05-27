# email_service.py

import os
import resend

resend.api_key = os.environ["RESEND_API_KEY"]

FROM_EMAIL = os.environ["EMAIL_FROM"]

def send_otp_email(email, otp):
    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": email,
        "subject": "Your Verification Code for Waveform SignUp",
        "html": f"""
        <h2>Your OTP Code</h2>
        <p>{otp}</p>
        <p>Expires in 10 minutes.</p>
        """
    })