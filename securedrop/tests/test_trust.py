import pytest
import os
import tempfile
import shutil

import pretty_bad_protocol as gnupg
from pretty_bad_protocol.gnupg import GPG

@pytest.fixture
def gpg_home():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir

@pytest.fixture
def gpg_instance(gpg_home):
    return GPG(homedir=gpg_home)

def test_export_ownertrust_with_existing_file(gpg_instance, gpg_home):
    # Given: an existing trustdb.gpg file
    existing_trustdb = os.path.join(gpg_home, "trustdb.gpg")
    with open(existing_trustdb, "w") as f:
        f.write("Existing trustdb content")

    # When: export_ownertrust is called
    gpg_instance.export_ownertrust()

    # Then: the original file is renamed and a new file is created
    assert os.path.exists(os.path.join(gpg_home, "trustdb.gpg.bak")), "Original file should be renamed to trustdb.gpg.bak"
    assert os.path.exists(existing_trustdb), "New trustdb.gpg file should exist"
    with open(existing_trustdb, "r") as f:
        content = f.read()
    assert content != "Existing trustdb content", "New trustdb.gpg should have different content"

def test_export_ownertrust_without_existing_file(gpg_instance, gpg_home):
    # Given: no existing trustdb.gpg file
    existing_trustdb = os.path.join(gpg_home, "trustdb.gpg")
    if os.path.exists(existing_trustdb):
        os.remove(existing_trustdb)
    assert not os.path.exists(existing_trustdb), "trustdb.gpg should not exist initially"

    # When: export_ownertrust is called
    gpg_instance.export_ownertrust()

    # Then: a new trustdb.gpg file is created without a backup
    assert os.path.exists(existing_trustdb), "New trustdb.gpg file should be created"
    assert not os.path.exists(os.path.join(gpg_home, "trustdb.gpg.bak")), "No backup file should be created"

def test_export_ownertrust_with_readonly_file(gpg_instance, gpg_home):
    # Given: a read-only trustdb.gpg file
    existing_trustdb = os.path.join(gpg_home, "trustdb.gpg")
    with open(existing_trustdb, "w") as f:
        f.write("Read-only trustdb content")
    os.chmod(existing_trustdb, 0o444)  # Set file as read-only

    # When: export_ownertrust is called
    gpg_instance.export_ownertrust()

    # Then: the original file is renamed to .bak and a new file is created
    backup_file = os.path.join(gpg_home, "trustdb.gpg.bak")
    assert os.path.exists(backup_file), "Original file should be renamed to trustdb.gpg.bak"
    assert os.path.exists(existing_trustdb), "New trustdb.gpg file should be created"
    
    # Verify that the backup file contains the original content
    with open(backup_file, "r") as f:
        backup_content = f.read()
    assert backup_content == "Read-only trustdb content", "Backup file should contain the original content"
    
    # Clean up: restore write permissions for cleanup
    os.chmod(existing_trustdb, 0o644)
    os.chmod(backup_file, 0o644)
