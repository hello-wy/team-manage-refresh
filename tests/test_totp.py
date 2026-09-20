import unittest

from app.utils.totp import TotpError, generate_totp, normalize_totp_secret


class TotpTests(unittest.TestCase):
    def test_rfc_6238_sha1_vector(self):
        secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
        self.assertEqual(generate_totp(secret, timestamp=59, digits=8), "94287082")

    def test_normalizes_otpauth_url(self):
        value = "otpauth://totp/Test?secret=jbsw%20y3dp-ehpk3pxp"
        self.assertEqual(normalize_totp_secret(value), "JBSWY3DPEHPK3PXP")

    def test_invalid_secret_is_explicit(self):
        with self.assertRaisesRegex(TotpError, "Base32"):
            generate_totp("INVALID*SECRET", timestamp=0)
