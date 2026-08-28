#!/usr/bin/env python3
"""
Tests for Redis worker key migration script.

This module tests the migration script that converts Redis keys from the old
inconsistent structure to the new hierarchical worker key structure.
"""

import json
import pytest
import tempfile
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime

# Add the project root to the Python path
import sys
sys.path.insert(0, '/app')

from scripts.migrate_redis_worker_keys import RedisWorkerKeyMigrator


class TestRedisWorkerKeyMigrator:
    """Test cases for the Redis worker key migration script."""
    
    @pytest.fixture
    def mock_redis_client(self):
        """Mock Redis client for testing."""
        mock_client = Mock()
        mock_client.keys.return_value = []
        mock_client.exists.return_value = False
        mock_client.type.return_value = b"hash"
        mock_client.hgetall.return_value = {}
        mock_client.smembers.return_value = set()
        mock_client.get.return_value = None
        mock_client.ttl.return_value = -1
        mock_client.setex.return_value = True
        mock_client.hset.return_value = True
        mock_client.sadd.return_value = True
        mock_client.set.return_value = True
        mock_client.delete.return_value = True
        mock_client.expire.return_value = True
        return mock_client
    
    @pytest.fixture
    def migrator(self, mock_redis_client):
        """Create a migrator instance with mocked Redis client."""
        with patch('scripts.migrate_redis_worker_keys.get_sync_redis_client', return_value=mock_redis_client):
            return RedisWorkerKeyMigrator(dry_run=True)
    
    def test_migrator_initialization(self, migrator):
        """Test migrator initialization."""
        assert migrator.dry_run is True
        assert migrator.redis_client is not None
        assert migrator.migration_stats['keys_migrated'] == 0
        assert migrator.migration_stats['keys_skipped'] == 0
        assert migrator.migration_stats['errors'] == 0
        assert migrator.migration_stats['backup_created'] is False
        
        # Check migration mappings
        assert len(migrator.migration_mappings) == 12
        assert migrator.ready_workers_mapping == ("imf:ready_workers", "worker:ready", "set")
    
    def test_extract_worker_id_simple_patterns(self, migrator):
        """Test worker ID extraction from simple patterns."""
        # Test monitor_info pattern
        worker_id = migrator._extract_worker_id("monitor_info:worker1", "monitor_info:*")
        assert worker_id == "worker1"
        
        # Test shared_resources pattern
        worker_id = migrator._extract_worker_id("shared_resources:worker2", "shared_resources:*")
        assert worker_id == "worker2"
        
        # Test worker_utilization pattern
        worker_id = migrator._extract_worker_id("worker_utilization:worker3", "worker_utilization:*")
        assert worker_id == "worker3"
        
        # Test imf:workers pattern
        worker_id = migrator._extract_worker_id("imf:workers:worker4", "imf:workers:*")
        assert worker_id == "worker4"
        
        # Test lock patterns
        worker_id = migrator._extract_worker_id("monitor_lock:worker1", "monitor_lock:*")
        assert worker_id == "worker1"
        
        schedule_id = migrator._extract_worker_id("schedule_execution:schedule123", "schedule_execution:*")
        assert schedule_id == "schedule123"
        
        worker_id = migrator._extract_worker_id("worker_warmup:worker2", "worker_warmup:*")
        assert worker_id == "worker2"
        
        credential_id = migrator._extract_worker_id("vault:store:cred123", "vault:store:*")
        assert credential_id == "cred123"
        
        credential_id = migrator._extract_worker_id("vault:delete:cred456", "vault:delete:*")
        assert credential_id == "cred456"
        
        # Test readiness lock special case
        readiness_id = migrator._extract_worker_id("imf:worker_readiness:validation_lock", "imf:worker_readiness:validation_lock")
        assert readiness_id == "readiness"
    
    def test_extract_worker_id_nested_patterns(self, migrator):
        """Test worker ID extraction from nested patterns."""
        # Test imf:worker_state pattern
        worker_id = migrator._extract_worker_id("imf:worker_state:worker1:state_type", "imf:worker_state:*")
        assert worker_id == "worker1"
        
        # Test imf:metrics pattern
        worker_id = migrator._extract_worker_id("imf:metrics:worker2:metric_name", "imf:metrics:*")
        assert worker_id == "worker2"
    
    def test_extract_worker_id_invalid_patterns(self, migrator):
        """Test worker ID extraction with invalid patterns."""
        # Test with non-matching key
        worker_id = migrator._extract_worker_id("other_key:worker1", "monitor_info:*")
        assert worker_id is None
        
        # Test with malformed nested pattern
        worker_id = migrator._extract_worker_id("imf:worker_state:worker1", "imf:worker_state:*")
        assert worker_id is None
    
    def test_migrate_single_key_hash(self, migrator, mock_redis_client):
        """Test migration of a single hash key."""
        # Setup mock data
        mock_redis_client.hgetall.return_value = {
            b"worker_id": b"worker1",
            b"state": b"ready",
            b"capabilities": b"model_inference,tool_execution"
        }
        mock_redis_client.ttl.return_value = 300
        
        # Test migration
        result = migrator._migrate_single_key("monitor_info:worker1", "worker:monitor:worker1", "hash")
        
        # Verify result
        assert result is True
        assert migrator.migration_stats['keys_migrated'] == 0  # Dry run mode
        
        # In non-dry-run mode, these would be called
        # mock_redis_client.hset.assert_called_once()
        # mock_redis_client.expire.assert_called_once()
    
    def test_migrate_single_key_set(self, migrator, mock_redis_client):
        """Test migration of a single set key."""
        # Setup mock data
        mock_redis_client.smembers.return_value = {b"worker1", b"worker2", b"worker3"}
        mock_redis_client.ttl.return_value = 600
        
        # Test migration
        result = migrator._migrate_single_key("imf:ready_workers", "worker:ready", "set")
        
        # Verify result
        assert result is True
        assert migrator.migration_stats['keys_migrated'] == 0  # Dry run mode
    
    def test_migrate_single_key_string(self, migrator, mock_redis_client):
        """Test migration of a single string key."""
        # Setup mock data
        mock_redis_client.get.return_value = b'{"worker_id": "worker1", "status": "active"}'
        mock_redis_client.ttl.return_value = 120
        
        # Test migration
        result = migrator._migrate_single_key("imf:workers:worker1", "worker:registration:worker1", "json_string")
        
        # Verify result
        assert result is True
        assert migrator.migration_stats['keys_migrated'] == 0  # Dry run mode
    
    def test_migrate_single_key_empty_data(self, migrator, mock_redis_client):
        """Test migration of a key with empty data."""
        # Setup mock data to return empty
        mock_redis_client.hgetall.return_value = {}
        
        # Test migration
        result = migrator._migrate_single_key("monitor_info:worker1", "worker:monitor:worker1", "hash")
        
        # Verify result
        assert result is False
        assert migrator.migration_stats['keys_migrated'] == 0
    
    def test_migrate_single_key_error(self, migrator, mock_redis_client):
        """Test migration error handling."""
        # Setup mock to raise exception
        mock_redis_client.hgetall.side_effect = Exception("Redis connection error")
        
        # Test migration
        result = migrator._migrate_single_key("monitor_info:worker1", "worker:monitor:worker1", "hash")
        
        # Verify result
        assert result is False
        assert migrator.migration_stats['errors'] == 0  # Error not counted in dry run
    
    def test_create_backup(self, migrator, mock_redis_client):
        """Test backup creation."""
        # Setup mock data
        mock_redis_client.keys.side_effect = [
            ["monitor_info:worker1", "shared_resources:worker1"],  # First call
            ["worker_utilization:worker1"],  # Second call
            ["imf:workers:worker1"],  # Third call
            ["imf:worker_state:worker1:state"],  # Fourth call
            ["imf:metrics:worker1:metric"],  # Fifth call
            ["monitor_lock:worker1"],  # Sixth call
            ["imf:worker_readiness:validation_lock"],  # Seventh call
            ["schedule_execution:schedule1"],  # Eighth call
            ["worker_warmup:worker1"],  # Ninth call
            ["vault:store:cred1"],  # Tenth call
            ["vault:delete:cred1"],  # Eleventh call
            []  # Twelfth call
        ]
        mock_redis_client.exists.return_value = True
        mock_redis_client.hgetall.return_value = {b"key": b"value"}
        mock_redis_client.smembers.return_value = {b"worker1"}
        mock_redis_client.get.return_value = b'{"data": "value"}'
        
        # Test backup creation
        backup_key = migrator.create_backup()
        
        # Verify backup key format
        assert backup_key.startswith("migration_backup:")
        assert len(backup_key) > len("migration_backup:")
        
        # Verify backup was created
        assert migrator.migration_stats['backup_created'] is True
    
    def test_restore_backup(self, migrator, mock_redis_client):
        """Test backup restoration."""
        # Setup mock backup data (convert bytes to strings for JSON serialization)
        backup_data = {
            "monitor_info:worker1": {
                "type": "hash",
                "data": {"worker_id": "worker1", "state": "ready"}
            },
            "imf:ready_workers": {
                "type": "set",
                "data": ["worker1", "worker2"]
            }
        }
        mock_redis_client.get.return_value = json.dumps(backup_data).encode()
        
        # Test restore
        result = migrator.restore_backup("migration_backup:test")
        
        # Verify result
        assert result is True
        assert migrator.migration_stats['backup_created'] is False  # Not creating backup during restore
    
    def test_restore_backup_not_found(self, migrator, mock_redis_client):
        """Test restore from non-existent backup."""
        # Setup mock to return None (backup not found)
        mock_redis_client.get.return_value = None
        
        # Test restore
        result = migrator.restore_backup("migration_backup:nonexistent")
        
        # Verify result
        assert result is False
    
    def test_migrate_pattern_keys(self, migrator, mock_redis_client):
        """Test migration of pattern-based keys."""
        # Setup mock data
        mock_redis_client.keys.return_value = ["monitor_info:worker1", "monitor_info:worker2"]
        mock_redis_client.exists.return_value = False  # New keys don't exist
        mock_redis_client.hgetall.return_value = {b"worker_id": b"worker1", b"state": b"ready"}
        mock_redis_client.ttl.return_value = 300
        
        # Test migration
        migrator._migrate_pattern_keys("monitor_info:*", "worker:monitor:*", "hash")
        
        # Verify stats
        assert migrator.migration_stats['keys_migrated'] == 2
        assert migrator.migration_stats['keys_skipped'] == 0
        assert migrator.migration_stats['errors'] == 0
    
    def test_migrate_pattern_keys_skip_existing(self, migrator, mock_redis_client):
        """Test migration skips existing new keys."""
        # Setup mock data
        mock_redis_client.keys.return_value = ["monitor_info:worker1"]
        mock_redis_client.exists.return_value = True  # New key already exists
        
        # Test migration
        migrator._migrate_pattern_keys("monitor_info:*", "worker:monitor:*", "hash")
        
        # Verify stats
        assert migrator.migration_stats['keys_migrated'] == 0
        assert migrator.migration_stats['keys_skipped'] == 1
        assert migrator.migration_stats['errors'] == 0
    
    def test_migrate_ready_workers(self, migrator, mock_redis_client):
        """Test migration of ready workers set."""
        # Setup mock data
        mock_redis_client.exists.return_value = True
        mock_redis_client.smembers.return_value = {b"worker1", b"worker2", b"worker3"}
        mock_redis_client.ttl.return_value = 600
        
        # Mock the new key doesn't exist
        def mock_exists(key):
            return key == "imf:ready_workers"  # Only old key exists
        
        mock_redis_client.exists.side_effect = mock_exists
        
        # Test migration
        migrator._migrate_ready_workers()
        
        # Verify stats
        assert migrator.migration_stats['keys_migrated'] == 1
        assert migrator.migration_stats['keys_skipped'] == 0
        assert migrator.migration_stats['errors'] == 0
    
    def test_migrate_ready_workers_not_exists(self, migrator, mock_redis_client):
        """Test migration when ready workers set doesn't exist."""
        # Setup mock data
        mock_redis_client.exists.return_value = False
        
        # Test migration
        migrator._migrate_ready_workers()
        
        # Verify stats (no change)
        assert migrator.migration_stats['keys_migrated'] == 0
        assert migrator.migration_stats['keys_skipped'] == 0
        assert migrator.migration_stats['errors'] == 0
    
    def test_cleanup_old_keys(self, migrator, mock_redis_client):
        """Test cleanup of old keys."""
        # Setup mock data
        mock_redis_client.keys.side_effect = [
            ["monitor_info:worker1", "monitor_info:worker2"],  # First pattern
            ["shared_resources:worker1"],  # Second pattern
            ["worker_utilization:worker1"],  # Third pattern
            ["imf:workers:worker1"],  # Fourth pattern
            ["imf:worker_state:worker1:state"],  # Fifth pattern
            ["imf:metrics:worker1:metric"],  # Sixth pattern
            ["monitor_lock:worker1"],  # Seventh pattern
            ["imf:worker_readiness:validation_lock"],  # Eighth pattern
            ["schedule_execution:schedule1"],  # Ninth pattern
            ["worker_warmup:worker1"],  # Tenth pattern
            ["vault:store:cred1"],  # Eleventh pattern
            ["vault:delete:cred1"],  # Twelfth pattern
        ]
        mock_redis_client.exists.return_value = True
        
        # Test cleanup
        result = migrator.cleanup_old_keys()
        
        # Verify result
        assert result is True
        # In dry run mode, delete should not be called
        assert mock_redis_client.delete.call_count == 0
    
    def test_cleanup_old_keys_error(self, migrator, mock_redis_client):
        """Test cleanup error handling."""
        # Setup mock to raise exception
        mock_redis_client.keys.side_effect = Exception("Redis connection error")
        
        # Test cleanup
        result = migrator.cleanup_old_keys()
        
        # Verify result
        assert result is False
    
    def test_print_migration_stats(self, migrator):
        """Test migration statistics printing."""
        # Set some test stats
        migrator.migration_stats = {
            'keys_migrated': 10,
            'keys_skipped': 2,
            'errors': 1,
            'backup_created': True
        }
        
        # Test printing (this uses logger, not print, so we can't capture with capsys)
        # Just verify the method doesn't raise an exception
        try:
            migrator._print_migration_stats()
            # If we get here, the method executed successfully
            assert True
        except Exception as e:
            assert False, f"Print migration stats failed: {e}"


class TestMigrationIntegration:
    """Integration tests for the migration process."""
    
    @pytest.fixture
    def mock_redis_with_data(self):
        """Mock Redis client with realistic data."""
        mock_client = Mock()
        
        # Setup keys for different patterns (need to provide responses for both backup and migration phases)
        mock_client.keys.side_effect = [
            # Backup phase (first 12 calls)
            ["monitor_info:worker1", "monitor_info:worker2"],  # monitor_info pattern
            ["shared_resources:worker1"],  # shared_resources pattern
            ["worker_utilization:worker1", "worker_utilization:worker2"],  # worker_utilization pattern
            ["imf:workers:worker1", "imf:workers:worker2"],  # imf:workers pattern
            ["imf:worker_state:worker1:state1", "imf:worker_state:worker2:state2"],  # imf:worker_state pattern
            ["imf:metrics:worker1:metric1", "imf:metrics:worker2:metric2"],  # imf:metrics pattern
            ["monitor_lock:worker1", "monitor_lock:worker2"],  # monitor_lock pattern
            ["imf:worker_readiness:validation_lock"],  # validation_lock pattern
            ["schedule_execution:schedule1", "schedule_execution:schedule2"],  # schedule_execution pattern
            ["worker_warmup:worker1", "worker_warmup:worker2"],  # worker_warmup pattern
            ["vault:store:cred1", "vault:store:cred2"],  # vault:store pattern
            ["vault:delete:cred1", "vault:delete:cred2"],  # vault:delete pattern
            # Migration phase (next 12 calls)
            ["monitor_info:worker1", "monitor_info:worker2"],  # monitor_info pattern
            ["shared_resources:worker1"],  # shared_resources pattern
            ["worker_utilization:worker1", "worker_utilization:worker2"],  # worker_utilization pattern
            ["imf:workers:worker1", "imf:workers:worker2"],  # imf:workers pattern
            ["imf:worker_state:worker1:state1", "imf:worker_state:worker2:state2"],  # imf:worker_state pattern
            ["imf:metrics:worker1:metric1", "imf:metrics:worker2:metric2"],  # imf:metrics pattern
            ["monitor_lock:worker1", "monitor_lock:worker2"],  # monitor_lock pattern
            ["imf:worker_readiness:validation_lock"],  # validation_lock pattern
            ["schedule_execution:schedule1", "schedule_execution:schedule2"],  # schedule_execution pattern
            ["worker_warmup:worker1", "worker_warmup:worker2"],  # worker_warmup pattern
            ["vault:store:cred1", "vault:store:cred2"],  # vault:store pattern
            ["vault:delete:cred1", "vault:delete:cred2"],  # vault:delete pattern
        ]
        
        # Setup ready workers
        mock_client.exists.return_value = True
        
        # Setup proper type responses
        def mock_type(key):
            if "monitor_info" in key or "shared_resources" in key or "worker_utilization" in key or "imf:worker_state" in key or "imf:metrics" in key:
                return b"hash"
            elif "imf:workers" in key:
                return b"string"
            elif key == "imf:ready_workers":
                return b"set"
            return b"none"
        
        # Setup data for different key types
        def mock_hgetall(key):
            if "monitor_info" in key:
                return {b"worker_id": key.split(":")[1].encode(), b"state": b"ready"}
            elif "shared_resources" in key:
                return {b"worker_id": key.split(":")[1].encode(), b"resources": b"cpu,memory"}
            elif "worker_utilization" in key:
                return {b"worker_id": key.split(":")[1].encode(), b"utilization": b"0.5"}
            elif "imf:worker_state" in key:
                return {b"worker_id": key.split(":")[2].encode(), b"state_type": key.split(":")[3].encode()}
            elif "imf:metrics" in key:
                return {b"worker_id": key.split(":")[2].encode(), b"metric_name": key.split(":")[3].encode()}
            return {}
        
        def mock_get(key):
            if "imf:workers" in key:
                return json.dumps({
                    "worker_id": key.split(":")[2],
                    "state": "ready",
                    "capabilities": ["model_inference", "tool_execution"]
                }).encode()
            return None
        
        def mock_smembers(key):
            if key == "imf:ready_workers":
                return {b"worker1", b"worker2"}
            return set()
        
        mock_client.type.side_effect = mock_type
        mock_client.hgetall.side_effect = mock_hgetall
        mock_client.get.side_effect = mock_get
        mock_client.smembers.side_effect = mock_smembers
        mock_client.ttl.return_value = 300
        
        return mock_client
    
    def test_full_migration_process(self, mock_redis_with_data):
        """Test the complete migration process."""
        # Mock that new keys don't exist
        def mock_exists(key):
            # Only old keys exist, new keys don't exist
            return key in ["imf:ready_workers"]
        
        mock_redis_with_data.exists.side_effect = mock_exists
        
        with patch('scripts.migrate_redis_worker_keys.get_sync_redis_client', return_value=mock_redis_with_data):
            migrator = RedisWorkerKeyMigrator(dry_run=True)
            
            # Run migration
            result = migrator.migrate_keys()
            
            # Verify success
            assert result is True
            
            # Verify statistics
            assert migrator.migration_stats['keys_migrated'] > 0
            assert migrator.migration_stats['backup_created'] is True
            assert migrator.migration_stats['errors'] == 0
    
    def test_migration_with_errors(self):
        """Test migration with some errors."""
        mock_client = Mock()
        # Make the first few keys() calls succeed (for backup creation) but fail on subsequent calls
        call_count = 0
        def mock_keys(pattern):
            nonlocal call_count
            call_count += 1
            if call_count <= 12:  # Allow first 12 calls for backup creation
                return []
            else:
                raise Exception("Connection error")
        
        mock_client.keys.side_effect = mock_keys
        mock_client.exists.return_value = False  # No ready workers
        mock_client.type.return_value = b"hash"  # Default type
        
        with patch('scripts.migrate_redis_worker_keys.get_sync_redis_client', return_value=mock_client):
            migrator = RedisWorkerKeyMigrator(dry_run=True)
            
            # Run migration
            result = migrator.migrate_keys()
            
            # Verify failure
            assert result is False


if __name__ == "__main__":
    pytest.main([__file__])
