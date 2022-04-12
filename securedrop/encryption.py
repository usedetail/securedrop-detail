import typing
from distutils.version import StrictVersion
from io import StringIO, BytesIO
from pathlib import Path
from typing import Optional, Dict, List

import pretty_bad_protocol as gnupg
import os
import re

from datetime import date
from redis import Redis


if typing.TYPE_CHECKING:
    from source_user import SourceUser


def _monkey_patch_username_in_env() -> None:
    # To fix https://github.com/freedomofpress/securedrop/issues/78
    os.environ["USERNAME"] = "www-data"


def _monkey_patch_unknown_status_message() -> None:
    # To fix https://github.com/isislovecruft/python-gnupg/issues/250 with Focal gnupg
    gnupg._parsers.Verify.TRUST_LEVELS["DECRYPTION_COMPLIANCE_MODE"] = 23


def _monkey_patch_delete_handle_status() -> None:
    # To fix https://github.com/freedomofpress/securedrop/issues/4294
    def _updated_handle_status(self: gnupg._parsers.DeleteResult, key: str, value: str) -> None:
        """
        Parse a status code from the attached GnuPG process.
        :raises: :exc:`~exceptions.ValueError` if the status message is unknown.
        """
        if key in ("DELETE_PROBLEM", "KEY_CONSIDERED"):
            self.status = self.problem_reason.get(value, "Unknown error: %r" % value)
        elif key in ("PINENTRY_LAUNCHED"):
            self.status = key.replace("_", " ").lower()
        else:
            raise ValueError("Unknown status message: %r" % key)

    gnupg._parsers.DeleteResult._handle_status = _updated_handle_status


def _setup_monkey_patches_for_gnupg() -> None:
    _monkey_patch_username_in_env()
    _monkey_patch_unknown_status_message()
    _monkey_patch_delete_handle_status()


_setup_monkey_patches_for_gnupg()


class GpgKeyNotFoundError(Exception):
    pass


class GpgEncryptError(Exception):
    pass


class GpgDecryptError(Exception):
    pass


_default_encryption_mgr: Optional["EncryptionManager"] = None


class EncryptionManager:

    GPG_KEY_TYPE = "RSA"
    GPG_KEY_LENGTH = 4096

    # All reply keypairs will be "created" on the same day SecureDrop (then
    # Strongbox) was publicly released for the first time.
    # https://www.newyorker.com/news/news-desk/strongbox-and-aaron-swartz
    DEFAULT_KEY_CREATION_DATE = date(2013, 5, 14)

    # '0' is the magic value that tells GPG's batch key generation not
    # to set an expiration date.
    DEFAULT_KEY_EXPIRATION_DATE = "0"

    REDIS_FINGERPRINT_HASH = "sd/crypto-util/fingerprints"
    REDIS_KEY_HASH = "sd/crypto-util/keys"

    SOURCE_KEY_NAME = "Source Key"
    SOURCE_KEY_UID_RE = re.compile(r"(Source|Autogenerated) Key <[-A-Za-z0-9+/=_]+>")

    def __init__(self, gpg_key_dir: Path, journalist_key_fingerprint: str) -> None:
        self._gpg_key_dir = gpg_key_dir
        self._journalist_key_fingerprint = journalist_key_fingerprint
        self._redis = Redis(decode_responses=True)

        # Instantiate the "main" GPG binary
        gpg = gnupg.GPG(
            binary="gpg2",
            homedir=str(self._gpg_key_dir),
            options=["--trust-model direct"]
        )
        if StrictVersion(gpg.binary_version) >= StrictVersion("2.1"):
            # --pinentry-mode, required for SecureDrop on GPG 2.1.x+, was added in GPG 2.1.
            self._gpg = gnupg.GPG(
                binary="gpg2",
                homedir=str(gpg_key_dir),
                options=["--pinentry-mode loopback", "--trust-model direct"]
            )
        else:
            self._gpg = gpg

        # Instantiate the GPG binary to be used for key deletion: always delete keys without
        # invoking pinentry-mode=loopback
        # see: https://lists.gnupg.org/pipermail/gnupg-users/2016-May/055965.html
        self._gpg_for_key_deletion = gnupg.GPG(
            binary="gpg2",
            homedir=str(self._gpg_key_dir),
            options=["--yes", "--trust-model direct"]
        )

        # Ensure that the journalist public key has been previously imported in GPG
        try:
            self.get_journalist_public_key()
        except GpgKeyNotFoundError:
            raise EnvironmentError(
                f"The journalist public key with fingerprint {journalist_key_fingerprint}"
                f" has not been imported into GPG."
            )

    @classmethod
    def get_default(cls) -> "EncryptionManager":
        # Late import so the module can be used without a config.py in the parent folder
        from sdconfig import config

        global _default_encryption_mgr
        if _default_encryption_mgr is None:
            _default_encryption_mgr = cls(
                gpg_key_dir=Path(config.GPG_KEY_DIR),
                journalist_key_fingerprint=config.JOURNALIST_KEY,
            )
        return _default_encryption_mgr

    def generate_source_key_pair(self, source_user: "SourceUser") -> None:
        gen_key_input = self._gpg.gen_key_input(
            passphrase=source_user.gpg_secret,
            name_email=source_user.filesystem_id,
            key_type=self.GPG_KEY_TYPE,
            key_length=self.GPG_KEY_LENGTH,
            name_real=self.SOURCE_KEY_NAME,
            creation_date=self.DEFAULT_KEY_CREATION_DATE.isoformat(),
            expire_date=self.DEFAULT_KEY_EXPIRATION_DATE,
        )
        new_key = self._gpg.gen_key(gen_key_input)

        # Store the newly-created key's fingerprint in Redis for faster lookups
        self._save_key_fingerprint_to_redis(source_user.filesystem_id, str(new_key))

    def delete_source_key_pair(self, source_filesystem_id: str) -> None:
        source_key_fingerprint = self.get_source_key_fingerprint(source_filesystem_id)

        # The subkeys keyword argument deletes both secret and public keys
        self._gpg_for_key_deletion.delete_keys(source_key_fingerprint, secret=True, subkeys=True)

        self._redis.hdel(self.REDIS_KEY_HASH, source_key_fingerprint)
        self._redis.hdel(self.REDIS_FINGERPRINT_HASH, source_filesystem_id)

    def get_journalist_public_key(self) -> str:
        return self._get_public_key(self._journalist_key_fingerprint)

    def get_source_public_key(self, source_filesystem_id: str) -> str:
        source_key_fingerprint = self.get_source_key_fingerprint(source_filesystem_id)
        return self._get_public_key(source_key_fingerprint)

    def get_source_key_fingerprint(self, source_filesystem_id: str) -> str:
        source_key_fingerprint = self._redis.hget(self.REDIS_FINGERPRINT_HASH, source_filesystem_id)
        if source_key_fingerprint:
            return source_key_fingerprint

        # If the fingerprint was not in Redis, get it directly from GPG
        source_key_details = self._get_source_key_details(source_filesystem_id)
        source_key_fingerprint = source_key_details["fingerprint"]
        self._save_key_fingerprint_to_redis(source_filesystem_id, source_key_fingerprint)
        return source_key_fingerprint

    def encrypt_source_message(self, message_in: str, encrypted_message_path_out: Path) -> None:
        message_as_stream = StringIO(message_in)
        self._encrypt(
            # A submission is only encrypted for the journalist key
            using_keys_with_fingerprints=[self._journalist_key_fingerprint],
            plaintext_in=message_as_stream,
            ciphertext_path_out=encrypted_message_path_out,
        )

    def encrypt_source_file(self, file_in: typing.IO, encrypted_file_path_out: Path) -> None:
        self._encrypt(
            # A submission is only encrypted for the journalist key
            using_keys_with_fingerprints=[self._journalist_key_fingerprint],
            plaintext_in=file_in,
            ciphertext_path_out=encrypted_file_path_out,
        )

    def encrypt_journalist_reply(
        self, for_source_with_filesystem_id: str, reply_in: str, encrypted_reply_path_out: Path
    ) -> None:
        source_key_fingerprint = self.get_source_key_fingerprint(for_source_with_filesystem_id)
        reply_as_stream = StringIO(reply_in)
        self._encrypt(
            # A reply is encrypted for both the journalist key and the source key
            using_keys_with_fingerprints=[source_key_fingerprint, self._journalist_key_fingerprint],
            plaintext_in=reply_as_stream,
            ciphertext_path_out=encrypted_reply_path_out,
        )

    def decrypt_journalist_reply(self, for_source_user: "SourceUser", ciphertext_in: bytes) -> str:
        ciphertext_as_stream = BytesIO(ciphertext_in)
        out = self._gpg.decrypt_file(ciphertext_as_stream, passphrase=for_source_user.gpg_secret)
        if not out.ok:
            raise GpgDecryptError(out.stderr)

        return out.data.decode("utf-8")

    def _encrypt(
        self,
        using_keys_with_fingerprints: List[str],
        plaintext_in: typing.IO,
        ciphertext_path_out: Path,
    ) -> None:
        # Remove any spaces from provided fingerprints GPG outputs fingerprints
        # with spaces for readability, but requires the spaces to be removed
        # when using fingerprints to specify recipients.
        sanitized_key_fingerprints = [fpr.replace(" ", "") for fpr in using_keys_with_fingerprints]

        out = self._gpg.encrypt(
            plaintext_in,
            *sanitized_key_fingerprints,
            output=str(ciphertext_path_out),
            always_trust=True,
            armor=False,
        )
        if not out.ok:
            raise GpgEncryptError(out.stderr)

    def _get_source_key_details(self, source_filesystem_id: str) -> Dict[str, str]:
        for key in self._gpg.list_keys():
            for uid in key["uids"]:
                if source_filesystem_id in uid and self.SOURCE_KEY_UID_RE.match(uid):
                    return key
        raise GpgKeyNotFoundError()

    def _save_key_fingerprint_to_redis(
        self, source_filesystem_id: str, source_key_fingerprint: str
    ) -> None:
        self._redis.hset(self.REDIS_FINGERPRINT_HASH, source_filesystem_id, source_key_fingerprint)

    def _get_public_key(self, key_fingerprint: str) -> str:
        # First try to fetch the public key from Redis
        public_key = self._redis.hget(self.REDIS_KEY_HASH, key_fingerprint)
        if public_key:
            return public_key

        # Then directly from GPG
        public_key = self._gpg.export_keys(key_fingerprint)
        if not public_key:
            raise GpgKeyNotFoundError()

        self._redis.hset(self.REDIS_KEY_HASH, key_fingerprint, public_key)
        return public_key
