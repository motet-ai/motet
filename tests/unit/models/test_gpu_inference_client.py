"""
Unit tests for GPU Inference Client (ADR-0042)
"""

import pytest
from unittest.mock import Mock, MagicMock
import json

from motet.core.models.local import LocalInferenceClient


class TestLocalInferenceClient:
    """Test LocalInferenceClient functionality"""
    
    def test_initialization(self):
        """Test client initialization"""
        redis_mock = Mock()
        client = LocalInferenceClient(redis_mock)
        
        assert client.redis == redis_mock
        assert client.worker_id is not None
    
    def test_infer_success(self):
        """Test successful inference request"""
        redis_mock = Mock()
        
        response_data = {
            'success': True,
            'result': {'text': 'Hello, world!'},
            'model_id': 'test-model',
            'elapsed_seconds': 0.5,
            'request_id': 'test-123'
        }
        redis_mock.xread.return_value = [
            (b'local-inference:unknown:responses:test-123', [
                (b'1-0', {
                    b'data': json.dumps(response_data).encode()
                })
            ])
        ]
        
        client = LocalInferenceClient(redis_mock)
        
        response = client.infer(
            model_id="test-model",
            messages=[{"role": "user", "content": "Hello"}],
            temperature=0.7,
            max_tokens=100
        )
        
        assert response['success'] is True
        assert response['result']['text'] == 'Hello, world!'
        assert response['model_id'] == 'test-model'
        
        assert redis_mock.xadd.called
        assert redis_mock.xread.called
        assert redis_mock.delete.called
    
    def test_infer_timeout(self):
        """Test inference timeout"""
        redis_mock = Mock()
        redis_mock.xread.return_value = []  # No response
        
        client = LocalInferenceClient(redis_mock)
        
        with pytest.raises(TimeoutError):
            client.infer(
                model_id="test-model",
                messages=[{"role": "user", "content": "Hello"}],
                timeout=0.1  # Very short timeout
            )
    
    def test_infer_failure(self):
        """Test inference failure"""
        redis_mock = Mock()
        
        redis_mock.xread.return_value = [
            (b'local-inference:unknown:responses:test-123', [
                (b'1-0', {
                    b'data': json.dumps({
                        'success': False,
                        'error': 'Model not found',
                        'request_id': 'test-123'
                    }).encode()
                })
            ])
        ]
        
        client = LocalInferenceClient(redis_mock)
        
        with pytest.raises(RuntimeError, match="Local inference failed: Model not found"):
            client.infer(
                model_id="nonexistent-model",
                messages=[{"role": "user", "content": "Hello"}]
            )
    
    def test_infer_sync_alias(self):
        """Test that infer_sync is an alias for infer"""
        redis_mock = Mock()
        redis_mock.xread.return_value = [
            (b'local-inference:unknown:responses:test-123', [
                (b'1-0', {
                    b'data': json.dumps({
                        'success': True,
                        'result': {'text': 'Test'},
                        'request_id': 'test-123'
                    }).encode()
                })
            ])
        ]
        
        client = LocalInferenceClient(redis_mock)
        
        response = client.infer_sync(
            model_id="test-model",
            messages=[{"role": "user", "content": "Hello"}]
        )
        
        assert response['success'] is True


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

