"""What the incident page shows about an attachment, and how files are served."""
import boto3
import pytest
from botocore.config import Config

KEY = "ticket-1/photo.png"


@pytest.fixture
def incident(site, alice):
    site.add_ticket("With a file", alice.id, attachment=KEY)
    return alice


def test_accepted_image_offers_a_viewer_and_a_thumbnail(incident, aws):
    aws.table.set(KEY, "accepted")
    html = incident.text("/incidents/1")
    assert "Accepted" in html and "View image" in html and 'class="thumb"' in html
    assert 'id="viewer"' in html and 'id="viewer-close"' in html and "Back to incident" in html


def test_rejected_file_shows_the_reason_and_no_link(incident, aws):
    aws.table.set(KEY, "rejected", "File type .exe not allowed")
    html = incident.text("/incidents/1")
    assert "Rejected" in html and ".exe not allowed" in html and "files.example" not in html


def test_file_still_being_checked(incident, aws):
    assert "Checking" in incident.text("/incidents/1")


def test_page_still_works_when_the_result_cannot_be_read(incident, aws):
    aws.table.denied = True
    response = incident.get("/incidents/1")
    assert response.status_code == 200 and "not available" in response.get_data(as_text=True)


def test_non_image_gets_an_open_link_but_no_thumbnail(site, alice, aws):
    site.add_ticket("Log file", alice.id, attachment="ticket-1/log.txt")
    aws.table.set("ticket-1/log.txt", "accepted")
    html = alice.text("/incidents/1")
    assert "Open file" in html and 'class="thumb"' not in html


class TestSignedLinks:
    """Uses a real boto3 client with dummy keys: signing needs no network."""

    @pytest.fixture
    def real_s3(self, clouddesk, aws, monkeypatch):
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="AKIATESTTESTTEST",
                              aws_secret_access_key="secret", config=Config(signature_version="s3v4"))
        monkeypatch.setattr(clouddesk, "s3", client)

    def link(self, clouddesk, aws, key):
        aws.table.set(key, "accepted")
        return clouddesk.attachment_info(key)

    def test_image_link_is_inline_typed_signed_and_short_lived(self, clouddesk, aws, real_s3):
        info = self.link(clouddesk, aws, "ticket-1/photo.png")
        assert info["kind"] == "image"
        assert "response-content-disposition=inline" in info["url"]
        assert "response-content-type=image%2Fpng" in info["url"]
        assert "X-Amz-Signature=" in info["url"] and "X-Amz-Expires=900" in info["url"]

    def test_text_file_is_served_as_plain_text(self, clouddesk, aws, real_s3):
        assert "response-content-type=text%2Fplain" in self.link(clouddesk, aws, "ticket-1/log.txt")["url"]

    def test_pdf_is_served_as_pdf(self, clouddesk, aws, real_s3):
        assert "response-content-type=application%2Fpdf" in self.link(clouddesk, aws, "ticket-1/doc.pdf")["url"]

    @pytest.mark.parametrize("name", ["evil.html", "page.svg", "run.exe", "noextension"])
    def test_types_that_could_run_code_never_get_a_link(self, clouddesk, aws, real_s3, name):
        assert self.link(clouddesk, aws, f"ticket-1/{name}")["url"] is None


def test_link_parameters_asked_of_s3(clouddesk, aws):
    aws.table.set(KEY, "accepted")
    clouddesk.attachment_info(KEY)
    operation, params, expires = aws.s3.presign_calls[0]
    assert operation == "get_object" and expires == 900
    assert params["ResponseContentDisposition"] == "inline" and params["ResponseContentType"] == "image/png"
