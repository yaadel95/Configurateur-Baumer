import unittest
from unittest.mock import patch

from canopen_client import CanopenClient, CanopenConfig


class FakeMessage:
    def __init__(self, arbitration_id, data):
        self.arbitration_id = arbitration_id
        self.data = data


class FakeBus:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def recv(self, timeout=None):
        return FakeMessage(0x580 + 1, bytes([0x4B, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00]))

    def shutdown(self):
        pass


class CanopenClientTimeoutTests(unittest.TestCase):
    def test_start_remote_node_and_sdo_read(self):
        fake_can_module = type("CanModule", (), {})()
        fake_can_module.interface = type("Interface", (), {})()
        fake_bus = FakeBus()
        fake_can_module.interface.Bus = lambda *args, **kwargs: fake_bus

        with patch("canopen_client.can", fake_can_module):
            client = CanopenClient(CanopenConfig(node_id=1, sdo_timeout=0.2))
            client.connect()
            client.start_remote_node()
            value = client.sdo_read_u16(0x1017, 0x00)

        self.assertEqual(value, 0)
        self.assertTrue(any(msg.arbitration_id == 0x000 for msg in fake_bus.sent))


if __name__ == "__main__":
    unittest.main()
