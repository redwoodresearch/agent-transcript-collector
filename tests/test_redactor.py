"""Tests for the redactor — verify precision (catches secrets) and recall (doesn't over-redact)."""

from claude_transcript_collector.redactor import redact, redact_jsonl_content


class TestRedactsSecrets:
    def test_aws_access_key(self):
        text = "key is AKIAIOSFODNN7EXAMPLE"
        result, records = redact(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in result
        assert "[REDACTED]" in result
        assert len(records) == 1
        assert records[0]["pattern_name"] == "AWS Access Key"

    def test_aws_asia_key(self):
        text = "ASIA1234567890ABCDEF"
        result, _ = redact(text)
        assert "ASIA1234567890ABCDEF" not in result

    def test_aws_secret_key(self):
        text = 'aws_secret_access_key = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"'
        result, records = redact(text)
        assert "wJalrXUtnFEMI" not in result
        assert len(records) == 1

    def test_sk_api_key(self):
        text = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456"
        result, _ = redact(text)
        assert "sk-ant-api03" not in result

    def test_openai_key(self):
        text = "sk-proj-abc123def456ghi789jkl012mno345"
        result, _ = redact(text)
        assert "sk-proj" not in result

    def test_github_token(self):
        text = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
        result, _ = redact(text)
        assert "ghp_" not in result

    def test_github_fine_grained_pat(self):
        text = "github_pat_abcdefghijklmnopqrstuvwxyz"
        result, _ = redact(text)
        assert "github_pat_" not in result

    def test_slack_token(self):
        text = "xoxb-123456789012-abcdefghij"
        result, _ = redact(text)
        assert "xoxb-" not in result

    def test_stripe_key(self):
        text = "sk_live_EXAMPLEKEYDONOTUSE12345"
        result, _ = redact(text)
        assert "sk_live_" not in result

    def test_jwt(self):
        text = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        result, _ = redact(text)
        assert "eyJhbGci" not in result

    def test_pem_private_key(self):
        text = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIBAAK\n-----END RSA PRIVATE KEY-----"
        result, _ = redact(text)
        assert "BEGIN RSA PRIVATE KEY" not in result

    def test_url_password(self):
        text = "postgres://admin:s3cretP4ss@db.example.com/mydb"
        result, _ = redact(text)
        assert "s3cretP4ss" not in result

    def test_password_assignment(self):
        text = 'password = "my_super_secret_123"'
        result, _ = redact(text)
        assert "my_super_secret_123" not in result

    def test_token_assignment(self):
        text = "api_key: 'long_token_value_here_abcdef'"
        result, _ = redact(text)
        assert "long_token_value_here" not in result


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
        assert "sk-ant" not in result
        assert result.count("[REDACTED]") >= 1
