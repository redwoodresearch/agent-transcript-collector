"""Tests for the redactor — verify precision (catches secrets) and recall (doesn't over-redact).

Secrets are replaced with type-preserving MOCKS (not a blanket [REDACTED]): the
real value never survives, but the type/structure does, and every mock embeds
the `_MOCK_TAG` marker (hex of "MOCK").
"""

import re

from claude_transcript_collector.redactor import _MOCK_TAG, redact, redact_jsonl_content

MOCK = re.compile(_MOCK_TAG, re.IGNORECASE)


class TestRedactsSecrets:
    def test_aws_access_key(self):
        text = "key is AKIAIOSFODNN7EXAMPLE"
        result, records = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert MOCK.search(result)
        assert len(records) == 1
        assert records[0]["pattern_name"] == "aws_access"
        # type preserved: still classifies as an AWS access key
        assert re.search(r"\bAKIA[A-Z0-9]{16}\b", result)

    def test_aws_asia_key(self):
        text = "ASIA1234567890ABCDEF"
        result, _ = redact(text)
        assert "ASIA1234567890ABCDEF" not in result
        assert result.startswith("ASIA")            # ASIA prefix preserved

    def test_aws_secret_key(self):
        text = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        result, records = redact(text)
        assert "wJalrXUtnFEMI" not in result
        assert len(records) == 1
        assert 'aws_secret_access_key = "' in result  # assignment context preserved

    def test_sk_api_key(self):
        text = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"
        result, _ = redact(text)
        assert "api03-abcdefghijklmnopqrstuvwxyz123456" not in result
        assert result.startswith("sk-ant-")          # Anthropic type preserved

    def test_openai_key(self):
        text = "sk-proj-abc123def456ghi789jkl012mno345"
        result, _ = redact(text)
        assert "proj-abc123def456" not in result
        assert result.startswith("sk-")

    def test_github_token(self):
        text = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
        result, _ = redact(text)
        assert "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl" not in result
        assert result.startswith("ghp_")             # GitHub token prefix preserved
        assert re.fullmatch(r"ghp_[a-zA-Z0-9]{36,}", result)

    def test_github_fine_grained_pat(self):
        text = "github_pat_abcdefghijklmnopqrstuvwxyz"
        result, _ = redact(text)
        assert "abcdefghijklmnopqrstuvwxyz" not in result
        assert result.startswith("github_pat_")

    def test_slack_token(self):
        text = "xoxb-123456789012-abcdefghij"
        result, _ = redact(text)
        assert "123456789012-abcdefghij" not in result
        assert result.startswith("xoxb-")            # Slack subtype preserved

    def test_stripe_key(self):
        text = "sk_live_EXAMPLEKEYDONOTUSE12345"
        result, _ = redact(text)
        assert "EXAMPLEKEYDONOTUSE12345" not in result
        assert result.startswith("sk_live_")

    def test_jwt(self):
        text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result, _ = redact(text)
        assert "eyJzdWIiOiIxMjM0NTY3ODkwIn0" not in result
        assert result.count(".") == 2 and result.startswith("eyJ")  # still a JWT shape

    def test_pem_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK\n-----END RSA PRIVATE KEY-----"
        result, _ = redact(text)
        assert "MIIEpAIBAAK" not in result
        assert "BEGIN RSA PRIVATE KEY" in result      # algo/type preserved, body mocked

    def test_url_password(self):
        text = "postgres://admin:s3cretP4ss@db.example.com/mydb"
        result, _ = redact(text)
        assert "s3cretP4ss" not in result
        assert "@db.example.com/mydb" in result       # host preserved

    def test_url_basic_auth_username_also_mocked(self):
        # Non-DB scheme: the username slot (a token-as-user) must not survive.
        text = "https://my_secret_token:hunter2@api.example.com"
        result, _ = redact(text)
        assert "my_secret_token" not in result and "hunter2" not in result
        assert "@api.example.com" in result            # host preserved

    def test_password_assignment(self):
        text = 'password = "my_super_secret_123"'
        result, _ = redact(text)
        assert "my_super_secret_123" not in result
        assert 'password = "' in result               # assignment context preserved

    def test_token_assignment(self):
        text = "api_key: 'long_token_value_here_abcdef'"
        result, _ = redact(text)
        assert "long_token_value_here" not in result

    def test_same_secret_maps_to_same_mock(self):
        # Stable within a run -> a secret's flow can be traced across the transcript.
        text = "export K=AKIAIOSFODNN7EXAMPLE\nlater again AKIAIOSFODNN7EXAMPLE"
        result, _ = redact(text)
        mocks = re.findall(r"AKIA[A-Z0-9]{16}", result)
        assert len(mocks) == 2 and mocks[0] == mocks[1]

    def test_different_secrets_map_to_different_mocks(self):
        result, _ = redact("AKIAIOSFODNN7EXAMPLE and AKIA1234567890ABCDEF1")
        mocks = re.findall(r"AKIA[A-Z0-9]{16}", result)
        assert len(mocks) == 2 and mocks[0] != mocks[1]


class TestDoesNotOverRedact:
    """These tests verify we don't redact normal content."""

    def test_normal_prose(self):
        text = "Can you help me write a function that parses JSON files?"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_hex_color(self):
        text = "background-color: #ff5733;"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_short_base64_in_code(self):
        text = 'encoding = base64.b64encode(data)'
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_uuid(self):
        text = "session id is 2e065f7f-706f-40e1-9404-1f744744abb1"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_git_hash(self):
        text = "commit abc123def456789"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_file_path(self):
        text = "/Users/someone/Documents/project/src/main.py"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_url_without_password(self):
        text = "https://api.example.com/v1/users?page=1"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_import_statement(self):
        text = "from sklearn.model_selection import train_test_split"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_python_dict(self):
        text = '{"name": "Alice", "age": 30, "city": "NYC"}'
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_error_message(self):
        text = "Error: ENOENT: no such file or directory, open '/tmp/test.txt'"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_markdown_code_block(self):
        text = "```python\ndef hello():\n    print('world')\n```"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_short_password_value_not_redacted(self):
        """Short values after 'password' shouldn't match (< 8 chars)."""
        text = 'password = "short"'
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_sk_short_not_redacted(self):
        """sk- followed by fewer than 20 chars is not an API key."""
        text = "sk-short"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_normal_word_starting_with_sk(self):
        text = "I want to skip this step and sketch the design"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_css_variable(self):
        text = "--accent-color: rgba(88, 166, 255, 0.08);"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_email_address(self):
        text = "Contact user@example.com for help"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_ip_address(self):
        text = "Server running at 192.168.1.100:8080"
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_typical_code_with_equals(self):
        text = 'max_retries = 3\ntimeout = 30\nbuffer_size = 4096'
        result, records = redact(text)
        assert result == text
        assert len(records) == 0

    def test_json_with_token_key_short_value(self):
        """A JSON field called 'token' with a short value shouldn't be redacted."""
        text = '{"token": "abc123"}'
        result, records = redact(text)
        assert result == text
        assert len(records) == 0


class TestRedactJsonlContent:
    def test_redacts_across_lines(self):
        content = '{"msg": "key is AKIAIOSFODNN7EXAMPLE"}\n{"msg": "ok"}\n'
        result, count = redact_jsonl_content(content)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert count == 1
        assert '"ok"' in result

    def test_no_redactions_returns_same(self):
        content = '{"msg": "hello world"}\n'
        result, count = redact_jsonl_content(content)
        assert result == content
        assert count == 0


class TestOverlappingRedactions:
    def test_overlapping_patterns_merged(self):
        text = 'aws_secret_access_key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456789012"'
        result, records = redact(text)
        assert "api03-abcdefghijklmnopqrstuvwxyz123456789012" not in result
        assert len(records) == 1                      # overlapping matches collapse to one mock
        assert MOCK.search(result)


class TestRedactsIdentity:
    def test_default_user_path_preserved(self):
        from claude_transcript_collector.redactor import redact_identity
        out, n = redact_identity("ran in /home/ubuntu/proj and /Users/Administrator/x", usernames=())
        assert "/home/ubuntu/proj" in out
        assert "/Users/Administrator/x" in out
        assert n == 0

    def test_real_user_path_redacted(self):
        from claude_transcript_collector.redactor import redact_identity
        out, n = redact_identity("cwd /home/nikolaskuhn/code", usernames=())
        assert "/home/nikolaskuhn/code" not in out
        assert "/home/[USER]/code" in out
        assert n == 1

    def test_bare_username_token_redacted(self):
        from claude_transcript_collector.redactor import redact_identity
        out, n = redact_identity("author: nikolaskuhn committed", usernames=("nikolaskuhn",))
        assert "nikolaskuhn" not in out
        assert "[USER]" in out

    def test_email_real_tld_redacted_decorator_preserved(self):
        from claude_transcript_collector.redactor import redact_identity
        out, n = redact_identity("mail me@anthropic.com via @dataclasses.dataclass", usernames=())
        assert "me@anthropic.com" not in out
        assert "[EMAIL]" in out
        assert "@dataclasses.dataclass" in out

    def test_stoplist_env_extension(self, monkeypatch):
        from claude_transcript_collector import redactor
        monkeypatch.setenv("CTC_USERNAME_STOPLIST", "buildbot")
        out, n = redactor.redact_identity("/home/buildbot/x", usernames=())
        assert "/home/buildbot/x" in out
        assert n == 0

    def test_uncommon_cctld_email_redacted(self):
        # Finding 2: fail safe — uncommon ccTLDs must not leak.
        from claude_transcript_collector.redactor import redact_identity
        out, n = redact_identity("ping colleague@firma.it now", usernames=())
        assert "colleague@firma.it" not in out
        assert "[EMAIL]" in out

    def test_decorator_at_escaped_newline_preserved(self):
        # Finding 2: `\n@module.attr` decorators in JSONL stay intact.
        from claude_transcript_collector.redactor import redact_identity
        out, _ = redact_identity("code\\n@dataclasses.dataclass and a@b.io", usernames=())
        assert "@dataclasses.dataclass" in out
        assert "a@b.io" not in out and "[EMAIL]" in out

    def test_internal_host_not_treated_as_email(self):
        from claude_transcript_collector.redactor import redact_identity
        out, _ = redact_identity("svc@ip-10-0-0-1.ec2.internal", usernames=())
        assert "ec2.internal" in out  # host-suffix denylist -> left alone

    def test_email_before_bare_token_ordering(self):
        # Finding 3: address whose local part is the username -> [EMAIL], not [USER]@...
        from claude_transcript_collector.redactor import redact_identity
        out, _ = redact_identity("reach nikolaskuhn@gmx.de", usernames=("nikolaskuhn",))
        assert "[EMAIL]" in out
        assert "[USER]@" not in out
        assert "gmx.de" not in out

    def test_path_fields_redacted_encoded_and_decoded(self):
        # Finding 1: the encoded group_key and decoded group_label both scrub.
        from claude_transcript_collector.redactor import redact_identity
        enc, _ = redact_identity("-home-nikolaskuhn-code", usernames=("nikolaskuhn",))
        assert "nikolaskuhn" not in enc and "[USER]" in enc
        dec, _ = redact_identity("/home/nikolaskuhn/code", usernames=())
        assert dec == "/home/[USER]/code"

    def test_encoded_path_key_redacted_any_username(self):
        # Finding 1: dash-encoded project keys scrub regardless of bare-token min length.
        from claude_transcript_collector.redactor import redact_path_token
        assert redact_path_token("-home-jo-proj", usernames=())[0] == "-home-[USER]-proj"
        assert redact_path_token("home-nikolaskuhn-code", usernames=())[0] == "home-[USER]-code"
        assert redact_path_token("-home-ubuntu-proj", usernames=())[0] == "-home-ubuntu-proj"
        assert redact_path_token("/home/jo/proj", usernames=())[0] == "/home/[USER]/proj"

    def test_decorator_in_diff_preserved(self):
        # Over-eager fix: @app.get/@app.post in diff/code lines are not emails.
        from claude_transcript_collector.redactor import redact_identity
        out, _ = redact_identity("-@app.get and +@app.post and x\\n@app.route", usernames=())
        assert "@app.get" in out and "@app.post" in out and "@app.route" in out
        assert "[EMAIL]" not in out

    def test_real_email_plus_localpart_still_redacted(self):
        from claude_transcript_collector.redactor import redact_identity
        out, _ = redact_identity("from 97564335+metatrot@users.noreply.github.com", usernames=())
        assert "metatrot" not in out and "[EMAIL]" in out

    def test_guest_is_default_username(self):
        from claude_transcript_collector.redactor import redact_path_token
        assert redact_path_token("/home/guest/x", usernames=())[0] == "/home/guest/x"


class TestRedactsCredentials:
    # Credentials must be caught by the SECRET pass (redact_jsonl_content),
    # independent of the identity/PII pass.
    def test_neon_token(self):
        out, n = redact_jsonl_content("db role npg_Ab12Cd34Ef56 here")
        assert "npg_Ab12Cd34Ef56" not in out and MOCK.search(out)
        assert "npg_" in out                         # Neon type preserved

    def test_neon_token_as_user_dsn(self):
        out, n = redact_jsonl_content("postgresql://npg_Ab12Cd34Ef56@ep-cool-1.neon.tech/db")
        assert "npg_Ab12Cd34Ef56" not in out
        assert "@ep-cool-1.neon.tech/db" in out      # host preserved

    def test_runpod_ssh(self):
        out, n = redact_jsonl_content("ssh abc12345-f0a1b2@ssh.runpod.io")
        assert "abc12345-f0a1b2" not in out and "@ssh.runpod.io" in out

    def test_runpod_ssh_uppercase_hex(self):
        out, n = redact_jsonl_content("ssh abc12345-F0A1B2@ssh.runpod.io")
        assert "abc12345-F0A1B2" not in out and "@ssh.runpod.io" in out

    def test_db_connection_uri_passwordless(self):
        out, n = redact_jsonl_content("redis://h7sometoken@cache.example/0")
        assert "h7sometoken" not in out

    def test_plain_text_not_over_redacted(self):
        # control: ordinary prose/code is untouched by the new patterns
        out, n = redact_jsonl_content("we ran postgres locally and it worked")
        assert out == "we ran postgres locally and it worked"
        assert n == 0
