"""
Fluent Plus
-----------
A lightweight desktop app to log in to Jenkins with an API token and run
test-case jobs, similar in spirit to the internal "Fluent" tool.

Built entirely with the Python standard library:
  - tkinter for the GUI
  - urllib for talking to the Jenkins REST API
  - a local JSON file for saving connection profiles (no database)

Run with:  python3 app.py
"""

import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

from jenkins_client import JenkinsClient, JenkinsAuthError, JenkinsConnectionError
import storage


POLL_INTERVAL_SECONDS = 3


# ======================================================================
# Login window
# ======================================================================
class LoginWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Fluent Plus - Login")
        self.geometry("420x360")
        self.resizable(False, False)
        self.client = None  # set on successful login

        self.profiles = storage.load_profiles()

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 12, "pady": 6}

        header = ttk.Label(self, text="Fluent Plus", font=("Segoe UI", 16, "bold"))
        header.pack(pady=(18, 0))
        sub = ttk.Label(self, text="Run Jenkins test cases", foreground="#666666")
        sub.pack(pady=(0, 12))

        form = ttk.Frame(self)
        form.pack(fill="x", **pad)

        # Saved profile picker
        ttk.Label(form, text="Saved profile").grid(row=0, column=0, sticky="w", pady=4)
        self.profile_var = tk.StringVar()
        profile_names = list(self.profiles.keys())
        self.profile_combo = ttk.Combobox(
            form, textvariable=self.profile_var, values=profile_names, state="readonly"
        )
        self.profile_combo.grid(row=0, column=1, sticky="ew", pady=4)
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)

        ttk.Label(form, text="Jenkins URL").grid(row=1, column=0, sticky="w", pady=4)
        self.url_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.url_var).grid(row=1, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="Username").grid(row=2, column=0, sticky="w", pady=4)
        self.user_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.user_var).grid(row=2, column=1, sticky="ew", pady=4)

        ttk.Label(form, text="API Token").grid(row=3, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.token_var, show="*").grid(row=3, column=1, sticky="ew", pady=4)

        form.columnconfigure(1, weight=1)

        self.remember_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            self, text="Save this profile locally", variable=self.remember_var
        ).pack(anchor="w", padx=12, pady=(4, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#b00020").pack(pady=(6, 0))

        self.login_btn = ttk.Button(self, text="Log In", command=self._on_login_clicked)
        self.login_btn.pack(pady=16, ipadx=10, ipady=4)

        hint = ttk.Label(
            self,
            text="Generate an API token at:\n<jenkins-url>/user/<you>/security/  (API Token section)",
            foreground="#888888",
            justify="center",
        )
        hint.pack(pady=(4, 0))

    def _on_profile_selected(self, _event=None):
        name = self.profile_var.get()
        p = self.profiles.get(name)
        if p:
            self.url_var.set(p["url"])
            self.user_var.set(p["username"])
            self.token_var.set(p["token"])

    def _on_login_clicked(self):
        url = self.url_var.get().strip()
        username = self.user_var.get().strip()
        token = self.token_var.get().strip()

        if not url or not username or not token:
            self.status_var.set("Please fill in URL, username, and API token.")
            return

        self.login_btn.config(state="disabled")
        self.status_var.set("Connecting...")
        self.update_idletasks()

        threading.Thread(
            target=self._attempt_login, args=(url, username, token), daemon=True
        ).start()

    def _attempt_login(self, url, username, token):
        client = JenkinsClient(url, username, token)
        try:
            client.verify_login()
        except JenkinsAuthError as e:
            self.after(0, self._login_failed, str(e))
            return
        except JenkinsConnectionError as e:
            self.after(0, self._login_failed, str(e))
            return
        except Exception as e:  # noqa: BLE001 - surface anything unexpected to the user
            self.after(0, self._login_failed, f"Unexpected error: {e}")
            return

        if self.remember_var.get():
            profile_name = f"{username}@{url}"
            storage.save_profile(profile_name, url, username, token)

        self.after(0, self._login_succeeded, client)

    def _login_failed(self, message):
        self.login_btn.config(state="normal")
        self.status_var.set(message)

    def _login_succeeded(self, client: JenkinsClient):
        self.client = client
        self.destroy()


# ======================================================================
# Main window
# ======================================================================
class MainWindow(tk.Tk):
    def __init__(self, client: JenkinsClient):
        super().__init__()
        self.client = client
        self.title(f"Fluent Plus - {client.username}@{client.base_url}")
        self.geometry("900x560")

        self.jobs = []
        self._build_ui()
        self._refresh_jobs()

    # ------------------------------------------------------------------
    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=8, pady=6)

        ttk.Button(toolbar, text="Refresh Jobs", command=self._refresh_jobs).pack(side="left")
        self.run_btn = ttk.Button(toolbar, text="Run Selected", command=self._run_selected)
        self.run_btn.pack(side="left", padx=(6, 0))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filter_jobs())
        ttk.Label(toolbar, text="Filter:").pack(side="left", padx=(20, 4))
        ttk.Entry(toolbar, textvariable=self.search_var, width=30).pack(side="left")

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # Job list (left)
        left = ttk.Frame(body)
        columns = ("status", "name")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("status", text="Status")
        self.tree.heading("name", text="Job Name")
        self.tree.column("status", width=90, anchor="center")
        self.tree.column("name", width=260)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._on_job_selected())
        body.add(left, weight=1)

        # Console / details (right)
        right = ttk.Frame(body)
        self.detail_label = ttk.Label(right, text="Select a job to see details", font=("Segoe UI", 10, "bold"))
        self.detail_label.pack(anchor="w", pady=(0, 4))

        self.console = tk.Text(right, wrap="none", state="disabled", bg="#111111", fg="#d0d0d0")
        self.console.pack(fill="both", expand=True)
        body.add(right, weight=2)

        self.status_bar_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_bar_var, anchor="w").pack(fill="x", padx=8, pady=(0, 4))

    # ------------------------------------------------------------------
    def _set_console(self, text):
        self.console.config(state="normal")
        self.console.delete("1.0", "end")
        self.console.insert("1.0", text)
        self.console.see("end")
        self.console.config(state="disabled")

    def _append_console(self, text):
        self.console.config(state="normal")
        self.console.insert("end", text)
        self.console.see("end")
        self.console.config(state="disabled")

    # ------------------------------------------------------------------
    def _refresh_jobs(self):
        self.status_bar_var.set("Loading jobs...")
        threading.Thread(target=self._load_jobs_thread, daemon=True).start()

    def _load_jobs_thread(self):
        try:
            jobs = self.client.list_jobs()
        except (JenkinsAuthError, JenkinsConnectionError) as e:
            self.after(0, lambda: messagebox.showerror("Error loading jobs", str(e)))
            self.after(0, lambda: self.status_bar_var.set("Failed to load jobs."))
            return
        self.after(0, self._populate_jobs, jobs)

    def _populate_jobs(self, jobs):
        self.jobs = jobs
        self.tree.delete(*self.tree.get_children())
        for job in jobs:
            color = job.get("color", "")
            status = self._color_to_status(color)
            self.tree.insert("", "end", iid=job["name"], values=(status, job["name"]))
        self.status_bar_var.set(f"Loaded {len(jobs)} job(s).")

    @staticmethod
    def _color_to_status(color: str) -> str:
        mapping = {
            "blue": "PASS",
            "blue_anime": "RUNNING",
            "red": "FAIL",
            "red_anime": "RUNNING",
            "yellow": "UNSTABLE",
            "yellow_anime": "RUNNING",
            "grey": "PENDING",
            "grey_anime": "RUNNING",
            "notbuilt": "NOT BUILT",
            "disabled": "DISABLED",
            "aborted": "ABORTED",
            "aborted_anime": "RUNNING",
        }
        return mapping.get(color, color or "UNKNOWN")

    def _filter_jobs(self):
        query = self.search_var.get().lower()
        self.tree.delete(*self.tree.get_children())
        for job in self.jobs:
            if query in job["name"].lower():
                status = self._color_to_status(job.get("color", ""))
                self.tree.insert("", "end", iid=job["name"], values=(status, job["name"]))

    # ------------------------------------------------------------------
    def _selected_job_name(self):
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _on_job_selected(self):
        name = self._selected_job_name()
        if not name:
            return
        self.detail_label.config(text=f"Job: {name}")
        self._set_console("")

    # ------------------------------------------------------------------
    def _run_selected(self):
        name = self._selected_job_name()
        if not name:
            messagebox.showinfo("No job selected", "Select a job from the list first.")
            return

        self.run_btn.config(state="disabled")
        self.status_bar_var.set(f"Fetching parameters for {name}...")
        threading.Thread(target=self._prepare_and_trigger, args=(name,), daemon=True).start()

    def _prepare_and_trigger(self, job_name):
        try:
            params_def = self.client.get_job_params(job_name)
        except (JenkinsAuthError, JenkinsConnectionError) as e:
            self.after(0, lambda: messagebox.showerror("Error", str(e)))
            self.after(0, lambda: self.run_btn.config(state="normal"))
            return

        if params_def:
            self.after(0, self._show_param_dialog, job_name, params_def)
        else:
            self.after(0, self._trigger_build, job_name, {})

    def _show_param_dialog(self, job_name, params_def):
        self.run_btn.config(state="normal")
        dialog = ParamDialog(self, job_name, params_def, on_submit=lambda p: self._trigger_build(job_name, p))
        dialog.grab_set()

    def _trigger_build(self, job_name, params):
        self.run_btn.config(state="disabled")
        self.status_bar_var.set(f"Triggering build for {job_name}...")
        self._set_console(f"Triggering {job_name}...\n")
        threading.Thread(target=self._trigger_thread, args=(job_name, params), daemon=True).start()

    def _trigger_thread(self, job_name, params):
        try:
            queue_url = self.client.trigger_build(job_name, params)
        except (JenkinsAuthError, JenkinsConnectionError) as e:
            self.after(0, lambda: messagebox.showerror("Error triggering build", str(e)))
            self.after(0, lambda: self.run_btn.config(state="normal"))
            return

        self.after(0, lambda: self._append_console("Queued. Waiting for build to start...\n"))
        self._poll_queue_then_build(job_name, queue_url)

    def _poll_queue_then_build(self, job_name, queue_url):
        build_number = None
        if queue_url:
            for _ in range(60):  # up to ~3 minutes waiting in queue
                try:
                    item = self.client.get_queue_item(queue_url)
                except (JenkinsAuthError, JenkinsConnectionError):
                    item = {}
                executable = item.get("executable")
                if executable:
                    build_number = executable.get("number")
                    break
                time.sleep(POLL_INTERVAL_SECONDS)

        if build_number is None:
            # Fallback: just grab whatever the latest build number is
            try:
                build_number = self.client.get_last_build_number(job_name)
            except (JenkinsAuthError, JenkinsConnectionError):
                build_number = None

        if build_number is None:
            self.after(0, lambda: self._append_console("Could not determine build number.\n"))
            self.after(0, lambda: self.run_btn.config(state="normal"))
            return

        self.after(0, lambda: self._append_console(f"Build #{build_number} started.\n\n"))
        self._poll_build_status(job_name, build_number)

    def _poll_build_status(self, job_name, build_number):
        last_len = 0
        while True:
            try:
                status = self.client.get_build_status(job_name, build_number)
                console = self.client.get_console_output(job_name, build_number)
            except (JenkinsAuthError, JenkinsConnectionError) as e:
                self.after(0, lambda: self._append_console(f"\n[Error polling build: {e}]\n"))
                break

            new_text = console[last_len:]
            if new_text:
                self.after(0, self._append_console, new_text)
                last_len = len(console)

            if not status.get("building", True):
                result = status.get("result", "UNKNOWN")
                self.after(0, lambda: self._append_console(f"\n--- Build finished: {result} ---\n"))
                self.after(0, lambda: self.status_bar_var.set(f"{job_name} #{build_number}: {result}"))
                self.after(0, self._refresh_jobs)
                break

            time.sleep(POLL_INTERVAL_SECONDS)

        self.after(0, lambda: self.run_btn.config(state="normal"))


# ======================================================================
# Parameter dialog (for parameterized Jenkins jobs)
# ======================================================================
class ParamDialog(tk.Toplevel):
    def __init__(self, parent, job_name, params_def, on_submit):
        super().__init__(parent)
        self.title(f"Parameters - {job_name}")
        self.geometry("360x400")
        self.on_submit = on_submit
        self.vars = {}

        ttk.Label(self, text=f"Set parameters for {job_name}", font=("Segoe UI", 10, "bold")).pack(pady=8)

        form = ttk.Frame(self)
        form.pack(fill="both", expand=True, padx=12)

        for i, pdef in enumerate(params_def):
            name = pdef["name"]
            ttk.Label(form, text=name).grid(row=i, column=0, sticky="w", pady=4)

            if pdef.get("choices"):
                var = tk.StringVar(value=pdef.get("default") or pdef["choices"][0])
                ttk.Combobox(
                    form, textvariable=var, values=pdef["choices"], state="readonly"
                ).grid(row=i, column=1, sticky="ew", pady=4)
            elif pdef.get("type") == "BooleanParameterDefinition":
                var = tk.BooleanVar(value=str(pdef.get("default")).lower() == "true")
                ttk.Checkbutton(form, variable=var).grid(row=i, column=1, sticky="w", pady=4)
            else:
                var = tk.StringVar(value=pdef.get("default") or "")
                ttk.Entry(form, textvariable=var).grid(row=i, column=1, sticky="ew", pady=4)

            self.vars[name] = var

        form.columnconfigure(1, weight=1)

        ttk.Button(self, text="Run Build", command=self._submit).pack(pady=12, ipadx=10)

    def _submit(self):
        params = {}
        for name, var in self.vars.items():
            value = var.get()
            if isinstance(value, bool):
                value = "true" if value else "false"
            params[name] = str(value)
        self.destroy()
        self.on_submit(params)


# ======================================================================
def main():
    login = LoginWindow()
    login.mainloop()

    if login.client is not None:
        main_win = MainWindow(login.client)
        main_win.mainloop()


if __name__ == "__main__":
    main()
