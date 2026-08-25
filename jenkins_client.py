"""
jenkins_client.py
------------------
A minimal Jenkins REST API client built entirely on the Python standard
library (urllib, json, base64). No third-party packages required.

Auth model: Jenkins "API Token" auth is just HTTP Basic Auth where the
password field is the API token instead of the account password.
Generate one at: <jenkins_url>/user/<username>/configure -> API Token.
"""

import base64
import json
import urllib.request
import urllib.error
import urllib.parse


class JenkinsAuthError(Exception):
    """Raised when Jenkins rejects the supplied credentials."""
    pass


class JenkinsConnectionError(Exception):
    """Raised when the Jenkins server can't be reached at all."""
    pass


class JenkinsClient:
    def __init__(self, base_url: str, username: str, api_token: str, timeout: int = 10):
        # Normalize URL - strip trailing slash
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.api_token = api_token
        self.timeout = timeout
        self._crumb_header = None  # CSRF crumb, fetched lazily if Jenkins requires it

    # ------------------------------------------------------------------
    # Low level request helper
    # ------------------------------------------------------------------
    def _build_request(self, url: str, method: str = "GET", data: bytes = None):
        req = urllib.request.Request(url, data=data, method=method)
        creds = f"{self.username}:{self.api_token}".encode("utf-8")
        auth_header = base64.b64encode(creds).decode("utf-8")
        req.add_header("Authorization", f"Basic {auth_header}")
        if self._crumb_header:
            req.add_header(self._crumb_header[0], self._crumb_header[1])
        return req

    def _request(self, path: str, method: str = "GET", data: bytes = None, raw: bool = False):
        url = f"{self.base_url}{path}"
        req = self._build_request(url, method=method, data=data)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
                if raw:
                    return body
                if not body:
                    return {}
                return json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise JenkinsAuthError(
                    f"Jenkins rejected the credentials (HTTP {e.code}). "
                    "Check the username and API token."
                )
            # Some Jenkins setups require a CSRF crumb for POST requests.
            if e.code == 403 and method == "POST":
                raise JenkinsAuthError("CSRF crumb required or invalid.")
            raise JenkinsConnectionError(f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise JenkinsConnectionError(f"Could not reach Jenkins: {e.reason}")

    # ------------------------------------------------------------------
    # Auth / crumb handling
    # ------------------------------------------------------------------
    def fetch_crumb(self):
        """Some Jenkins instances require a CSRF crumb for POST requests
        (triggering builds). Fetch it if available; silently skip if the
        instance doesn't use crumbs."""
        try:
            data = self._request("/crumbIssuer/api/json")
            if data and "crumbRequestField" in data and "crumb" in data:
                self._crumb_header = (data["crumbRequestField"], data["crumb"])
        except (JenkinsConnectionError, JenkinsAuthError):
            # Crumb issuer might be disabled - that's fine, not fatal.
            pass

    def verify_login(self):
        """Confirms the URL/username/token combination is valid.
        Raises JenkinsAuthError / JenkinsConnectionError on failure.
        Returns the Jenkins "whoAmI"-ish user info dict on success.
        """
        info = self._request(f"/user/{urllib.parse.quote(self.username)}/api/json")
        self.fetch_crumb()
        return info

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------
    def list_jobs(self):
        """Returns a list of dicts: [{name, url, color, buildable}, ...]"""
        data = self._request(
            "/api/json?tree=jobs[name,url,color,buildable]"
        )
        return data.get("jobs", [])

    def get_job_info(self, job_name: str):
        """Full info for one job, including parameter definitions if any."""
        path = f"/job/{urllib.parse.quote(job_name)}/api/json?tree=" \
               "name,url,buildable,color,lastBuild[number,url]," \
               "property[parameterDefinitions[name,type,defaultParameterValue[value],choices]]"
        return self._request(path)

    def get_job_params(self, job_name: str):
        """Extracts a simple list of parameter definitions for a job, if any."""
        info = self.get_job_info(job_name)
        params = []
        for prop in info.get("property", []):
            for pdef in prop.get("parameterDefinitions", []) or []:
                default = pdef.get("defaultParameterValue", {})
                params.append({
                    "name": pdef.get("name"),
                    "type": pdef.get("type"),
                    "default": default.get("value") if default else "",
                    "choices": pdef.get("choices"),
                })
        return params

    # ------------------------------------------------------------------
    # Triggering builds
    # ------------------------------------------------------------------
    def trigger_build(self, job_name: str, params: dict = None):
        """
        Triggers a build. If params is provided (non-empty dict), uses the
        buildWithParameters endpoint; otherwise uses the plain build endpoint.
        Returns the "queue item" URL from the Location header, if available.
        """
        job_path = f"/job/{urllib.parse.quote(job_name)}"
        if params:
            query = urllib.parse.urlencode(params)
            path = f"{job_path}/buildWithParameters?{query}"
        else:
            path = f"{job_path}/build"

        url = f"{self.base_url}{path}"
        req = self._build_request(url, method="POST", data=b"")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.headers.get("Location")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise JenkinsAuthError(f"Not authorized to trigger builds (HTTP {e.code}).")
            raise JenkinsConnectionError(f"HTTP {e.code}: {e.reason}")
        except urllib.error.URLError as e:
            raise JenkinsConnectionError(f"Could not reach Jenkins: {e.reason}")

    # ------------------------------------------------------------------
    # Queue / build status
    # ------------------------------------------------------------------
    def get_queue_item(self, queue_url: str):
        """queue_url is the absolute URL returned by trigger_build (e.g.
        http://jenkins/queue/item/123/). Returns queue item info, which
        includes an 'executable' key with the build number once it starts."""
        # queue_url already contains the base url; strip it to get the path
        if queue_url.startswith(self.base_url):
            path = queue_url[len(self.base_url):]
        else:
            path = queue_url
        if not path.endswith("/"):
            path += "/"
        return self._request(f"{path}api/json")

    def get_build_status(self, job_name: str, build_number):
        path = f"/job/{urllib.parse.quote(job_name)}/{build_number}/api/json"
        return self._request(path)

    def get_console_output(self, job_name: str, build_number):
        path = f"/job/{urllib.parse.quote(job_name)}/{build_number}/consoleText"
        raw = self._request(path, raw=True)
        return raw.decode("utf-8", errors="replace")

    def get_last_build_number(self, job_name: str):
        info = self._request(f"/job/{urllib.parse.quote(job_name)}/api/json?tree=lastBuild[number]")
        last = info.get("lastBuild")
        return last["number"] if last else None
