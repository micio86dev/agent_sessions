#!/usr/bin/env python3
import subprocess
import tkinter as tk
import sys
import os
import threading
import queue

AGENT_SCRIPT = os.path.join(os.path.dirname(__file__), "agent_sessions.py")
EXPORT_SCRIPT = os.path.join(os.path.dirname(__file__), "export_mock.py")


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Agent Controller")
        self.agent_process = None
        self.log_queue = queue.Queue()

        # Bottoni
        tk.Button(root, text="Start Agent", command=self.start_agent, width=20).pack(
            pady=5
        )
        tk.Button(root, text="Stop Agent", command=self.stop_agent, width=20).pack(
            pady=5
        )
        tk.Button(
            root, text="Export Mock JSON", command=self.export_mock, width=20
        ).pack(pady=5)

        # Label di log
        self.status_label = tk.Label(
            root, text="Status: pronto", anchor="w", justify="left"
        )
        self.status_label.pack(fill="x", padx=10, pady=10)

        # Aggiornamento log periodico
        self.update_log()

    def log(self, message):
        """Inserisce un messaggio nella coda dei log."""
        self.log_queue.put(message)

    def update_log(self):
        """Aggiorna la label con i messaggi della coda."""
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            self.status_label.config(text=msg)
        self.root.after(200, self.update_log)

    def start_agent(self):
        if self.agent_process is not None and self.agent_process.poll() is None:
            self.log("Status: Agent già in esecuzione")
            return

        def run_agent():
            self.agent_process = subprocess.Popen([sys.executable, AGENT_SCRIPT])
            self.log("Status: Agent avviato")
            self.agent_process.wait()
            self.log("Status: Agent terminato")
            self.agent_process = None

        threading.Thread(target=run_agent, daemon=True).start()

    def stop_agent(self):
        if self.agent_process and self.agent_process.poll() is None:
            self.agent_process.terminate()
            self.agent_process = None
            self.log("Status: Agent fermato")
        else:
            self.log("Status: Agent non era in esecuzione")

    def export_mock(self):
        def run_export():
            try:
                subprocess.run([sys.executable, EXPORT_SCRIPT], check=True)
                self.log("Status: Mock JSON esportato")
            except subprocess.CalledProcessError:
                self.log("Status: Errore durante esportazione")

        threading.Thread(target=run_export, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = App(root)
    root.mainloop()
