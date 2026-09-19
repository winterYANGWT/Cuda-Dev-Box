import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


HOST_DIR = Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST_DIR))

from devbox_lib import ssh_agent  # noqa: E402


class SshAgentProxyTests(unittest.TestCase):
    def test_proxy_relays_to_upstream_agent_and_stops_cleanly(self):
        with tempfile.TemporaryDirectory() as directory:
            upstream_path = os.path.join(directory, "upstream.sock")
            agent_dir = os.path.join(directory, "agent")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
                upstream.bind(upstream_path)
                upstream.listen(1)
                listen_sock = ssh_agent.start_proxy(agent_dir, upstream_path)
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(listen_sock)
                        client.sendall(b"ping")
                        connection, _ = upstream.accept()
                        with connection:
                            self.assertEqual(connection.recv(4), b"ping")
                finally:
                    ssh_agent.stop_proxy(agent_dir)
            for _ in range(40):
                if not os.path.exists(listen_sock):
                    break
                time.sleep(0.05)
            self.assertFalse(os.path.exists(listen_sock))

    def test_refresh_without_host_agent_fails_without_starting_proxy(self):
        with patch.dict(os.environ, {}, clear=True), patch("sys.stderr"), \
                patch.object(ssh_agent, "start_proxy") as start_proxy:
            self.assertEqual(ssh_agent.refresh_proxy(SimpleNamespace(container="dev")), 1)
        start_proxy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
