"""
测试 Config + Endpoint 集成
"""
import pytest
from autodoip import Config, Endpoint


class TestConfig:
    """Config 是普通 dataclass，所有字段有默认值"""

    def test_default_values(self):
        c = Config()
        assert c.accept_timeout == 1.5
        assert c.recv_timeout == 3.0
        assert c.reconnect_timeout == 5.0
        assert c.listen_count == 10
        assert c.version == 0x02
        assert c.msg_type == 0x8001
        assert c.byte_order == 'big'

    def test_partial_override(self):
        c = Config(recv_timeout=5.0, version=0x03)
        assert c.recv_timeout == 5.0
        assert c.version == 0x03
        assert c.accept_timeout == 1.5       # 未覆盖保持默认
        assert c.listen_count == 10

    def test_immutable_when_not_overridden(self):
        c1 = Config()
        c2 = Config()
        assert c1.listen_count == c2.listen_count == 10


class TestEndpointWithConfig:
    """Endpoint 不传 config → 内部 Config() 取默认值；传 config → 用传入的"""

    def test_default_config(self):
        ep = Endpoint(ip='0.0.0.0', ecus={0x1301: ('10.0.0.1', 0)})
        assert ep._config.accept_timeout == 1.5
        assert ep._config.listen_count == 10
        assert ep._config.byte_order == 'big'

    def test_custom_config_passed(self):
        cfg = Config(recv_timeout=9.9, listen_count=5)
        ep = Endpoint(ip='0.0.0.0', ecus={0x1301: ('10.0.0.1', 0)}, config=cfg)
        assert ep._config.recv_timeout == 9.9
        assert ep._config.listen_count == 5
        assert ep._config.accept_timeout == 1.5  # 未覆盖

    def test_endpoint_does_not_unpack_config_to_self(self):
        """self._config 存一个引用，不拆散成多个属性"""
        cfg = Config(version=0x03)
        ep = Endpoint(ip='0.0.0.0', ecus={}, config=cfg)
        assert ep._config is cfg
        assert not hasattr(ep, '_accept_timeout')
        assert not hasattr(ep, '_recv_timeout')
        assert not hasattr(ep, '_reconnect_timeout')
        assert not hasattr(ep, '_listen_count')

    def test_tester_in_endpoint_not_in_config(self):
        """tester 是身份参数，在 Endpoint 签名中，不在 Config"""
        ep = Endpoint(ip='0.0.0.0', ecus={}, tester=0x1234)
        assert ep._tester == 0x1234
        assert not hasattr(Config(), 'tester')

    def test_port_in_endpoint_not_in_config(self):
        """port 是身份参数，在 Endpoint 签名中，不在 Config"""
        ep = Endpoint(ip='0.0.0.0', ecus={}, port=20000)
        assert ep._port == 20000
        assert not hasattr(Config(), 'port')