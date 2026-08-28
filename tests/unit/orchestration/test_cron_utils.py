"""
Tests for Cron Expression Utilities

Tests the cron parsing, validation, and next execution calculation functionality.
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import patch

from motet.core.orchestration.scheduling.cron_utils import (
    validate_cron_expression,
    get_next_execution_from_cron,
    get_previous_execution_from_cron,
    describe_cron_expression,
    is_time_for_cron_execution,
    CronExpressionError
)


class TestCronValidation:
    """Test cron expression validation"""
    
    def test_valid_cron_expressions(self):
        """Test that valid cron expressions are accepted"""
        valid_expressions = [
            "* * * * *",  # Every minute
            "0 * * * *",  # Every hour
            "0 0 * * *",  # Every day at midnight
            "0 9 * * MON-FRI",  # Weekdays at 9 AM
            "*/5 * * * *",  # Every 5 minutes
            "0 */2 * * *",  # Every 2 hours
            "0 0 1 * *",  # First day of every month
            "0 0 * * 0",  # Every Sunday
            "30 14 * * 1-5",  # Weekdays at 2:30 PM
        ]
        
        for expr in valid_expressions:
            assert validate_cron_expression(expr), f"Should be valid: {expr}"
    
    def test_invalid_cron_expressions(self):
        """Test that invalid cron expressions are rejected"""
        invalid_expressions = [
            "",  # Empty string
            "* * * *",  # Too few fields (4 fields)
            "* * * * * * * *",  # Too many fields (8 fields)
            "60 * * * *",  # Invalid minute (0-59)
            "* 24 * * *",  # Invalid hour (0-23)
            "* * 32 * *",  # Invalid day (1-31)
            "* * * 13 *",  # Invalid month (1-12)
            "* * * * 8",  # Invalid weekday (0-7, but 8 is invalid)
            "invalid",  # Non-numeric
        ]
        
        for expr in invalid_expressions:
            assert not validate_cron_expression(expr), f"Should be invalid: {expr}"


class TestCronNextExecution:
    """Test next execution calculation from cron expressions"""
    
    def test_every_minute(self):
        """Test every minute cron expression"""
        base_time = datetime(2024, 1, 1, 12, 30, 0)  # 12:30:00
        next_execution = get_next_execution_from_cron("* * * * *", base_time)
        
        expected = datetime(2024, 1, 1, 12, 31, 0)  # 12:31:00
        assert next_execution == expected
    
    def test_every_hour(self):
        """Test every hour cron expression"""
        base_time = datetime(2024, 1, 1, 12, 30, 0)  # 12:30:00
        next_execution = get_next_execution_from_cron("0 * * * *", base_time)
        
        expected = datetime(2024, 1, 1, 13, 0, 0)  # 13:00:00
        assert next_execution == expected
    
    def test_daily_at_midnight(self):
        """Test daily at midnight cron expression"""
        base_time = datetime(2024, 1, 1, 12, 30, 0)  # 12:30:00
        next_execution = get_next_execution_from_cron("0 0 * * *", base_time)
        
        expected = datetime(2024, 1, 2, 0, 0, 0)  # Next day at midnight
        assert next_execution == expected
    
    def test_weekdays_at_9am(self):
        """Test weekdays at 9 AM cron expression"""
        # Start on a Sunday (weekday 6)
        base_time = datetime(2024, 1, 7, 12, 0, 0)  # Sunday 12:00
        next_execution = get_next_execution_from_cron("0 9 * * MON-FRI", base_time)
        
        # Should be Monday at 9 AM
        expected = datetime(2024, 1, 8, 9, 0, 0)  # Monday 9:00
        assert next_execution == expected
    
    def test_every_5_minutes(self):
        """Test every 5 minutes cron expression"""
        base_time = datetime(2024, 1, 1, 12, 32, 0)  # 12:32:00
        next_execution = get_next_execution_from_cron("*/5 * * * *", base_time)
        
        expected = datetime(2024, 1, 1, 12, 35, 0)  # 12:35:00
        assert next_execution == expected
    
    def test_invalid_cron_raises_error(self):
        """Test that invalid cron expressions raise CronExpressionError"""
        with pytest.raises(CronExpressionError):
            get_next_execution_from_cron("invalid cron", datetime.utcnow())
    
    def test_default_base_time(self):
        """Test that default base time is current UTC time"""
        with patch('motet.core.orchestration.scheduling.cron_utils.datetime') as mock_datetime:
            mock_now = datetime(2024, 1, 1, 12, 0, 0)
            mock_datetime.utcnow.return_value = mock_now
            
            # Mock croniter to return a predictable result
            with patch('motet.core.orchestration.scheduling.cron_utils.croniter') as mock_croniter:
                mock_cron_instance = mock_croniter.return_value
                expected_next = datetime(2024, 1, 1, 12, 1, 0)
                mock_cron_instance.get_next.return_value = expected_next
                
                result = get_next_execution_from_cron("* * * * *")
                
                # Verify croniter was called with the mocked current time
                mock_croniter.assert_called_once_with("* * * * *", mock_now)
                assert result == expected_next


class TestCronPreviousExecution:
    """Test previous execution calculation from cron expressions"""
    
    def test_every_minute_previous(self):
        """Test previous execution for every minute cron"""
        base_time = datetime(2024, 1, 1, 12, 30, 0)  # 12:30:00
        prev_execution = get_previous_execution_from_cron("* * * * *", base_time)
        
        expected = datetime(2024, 1, 1, 12, 29, 0)  # 12:29:00
        assert prev_execution == expected
    
    def test_every_hour_previous(self):
        """Test previous execution for every hour cron"""
        base_time = datetime(2024, 1, 1, 12, 30, 0)  # 12:30:00
        prev_execution = get_previous_execution_from_cron("0 * * * *", base_time)
        
        expected = datetime(2024, 1, 1, 12, 0, 0)  # 12:00:00
        assert prev_execution == expected


class TestCronDescription:
    """Test human-readable cron expression descriptions"""
    
    def test_common_expressions(self):
        """Test descriptions for common cron expressions"""
        test_cases = [
            ("* * * * *", "Every minute"),
            ("0 * * * *", "Every hour"),
            ("0 0 * * *", "Every day at midnight"),
            ("0 9 * * MON-FRI", "Weekdays at 9:00 AM"),
            ("*/5 * * * *", "Every 5 minutes"),
            ("*/10 * * * *", "Every 10 minutes"),
            ("0 */2 * * *", "Every 2 hours"),
        ]
        
        for expr, expected_desc in test_cases:
            desc = describe_cron_expression(expr)
            assert desc == expected_desc, f"Expression '{expr}' should be '{expected_desc}', got '{desc}'"
    
    def test_custom_expressions(self):
        """Test descriptions for custom cron expressions"""
        # Test a custom expression that should get a generated description
        desc = describe_cron_expression("15 14 * * 2")
        assert "at minute 15" in desc.lower()
        assert "at hour 14" in desc.lower()
        assert "tuesday" in desc.lower()
    
    def test_invalid_expression_description(self):
        """Test description for invalid cron expression"""
        desc = describe_cron_expression("invalid")
        assert "invalid" in desc.lower()


class TestCronExecutionTiming:
    """Test cron execution timing logic"""
    
    def test_time_for_execution_no_last_execution(self):
        """Test execution timing when there's no last execution"""
        current_time = datetime(2024, 1, 1, 12, 30, 0)
        
        # For every minute cron, if we're at 12:30:00 and the previous scheduled
        # time was 12:30:00 (within the last minute), we should execute
        with patch('motet.core.orchestration.scheduling.cron_utils.get_previous_execution_from_cron') as mock_prev:
            mock_prev.return_value = datetime(2024, 1, 1, 12, 30, 0)  # Exactly at current time
            
            result = is_time_for_cron_execution("* * * * *", None, current_time)
            assert result is True
    
    def test_time_for_execution_with_last_execution(self):
        """Test execution timing with a last execution time"""
        current_time = datetime(2024, 1, 1, 12, 30, 0)
        last_execution = datetime(2024, 1, 1, 12, 29, 0)
        
        with patch('motet.core.orchestration.scheduling.cron_utils.get_next_execution_from_cron') as mock_next:
            # Next execution should be at 12:30:00, which is now
            mock_next.return_value = datetime(2024, 1, 1, 12, 30, 0)
            
            result = is_time_for_cron_execution("* * * * *", last_execution, current_time)
            assert result is True
    
    def test_not_time_for_execution(self):
        """Test when it's not time for execution"""
        current_time = datetime(2024, 1, 1, 12, 30, 0)
        last_execution = datetime(2024, 1, 1, 12, 29, 0)
        
        with patch('motet.core.orchestration.scheduling.cron_utils.get_next_execution_from_cron') as mock_next:
            # Next execution should be in the future
            mock_next.return_value = datetime(2024, 1, 1, 12, 31, 0)
            
            result = is_time_for_cron_execution("* * * * *", last_execution, current_time)
            assert result is False
    
    def test_execution_timing_error_handling(self):
        """Test error handling in execution timing"""
        # Test with invalid cron expression
        result = is_time_for_cron_execution("invalid", None, datetime.utcnow())
        assert result is False


class TestCronIntegration:
    """Test integration scenarios with cron expressions"""
    
    def test_real_world_cron_expressions(self):
        """Test real-world cron expressions"""
        base_time = datetime(2024, 1, 1, 0, 0, 0)  # Start of year
        
        test_cases = [
            # Expression, description of what it should do
            ("0 0 * * *", "Daily at midnight"),
            ("0 12 * * *", "Daily at noon"),
            ("0 9 * * 1", "Every Monday at 9 AM"),
            ("0 0 1 * *", "First day of every month"),
            ("0 0 1 1 *", "New Year's Day"),
            ("*/15 * * * *", "Every 15 minutes"),
            ("0 */4 * * *", "Every 4 hours"),
        ]
        
        for expr, description in test_cases:
            # Validate the expression
            assert validate_cron_expression(expr), f"Invalid expression: {expr} ({description})"
            
            # Get next execution
            next_exec = get_next_execution_from_cron(expr, base_time)
            assert next_exec is not None, f"No next execution for: {expr} ({description})"
            assert next_exec > base_time, f"Next execution should be in future: {expr} ({description})"
            
            # Get description
            desc = describe_cron_expression(expr)
            assert len(desc) > 0, f"Empty description for: {expr} ({description})"
    
    def test_edge_cases(self):
        """Test edge cases in cron expressions"""
        # Test leap year handling
        leap_year_base = datetime(2024, 2, 28, 0, 0, 0)  # 2024 is a leap year
        next_exec = get_next_execution_from_cron("0 0 29 2 *", leap_year_base)
        assert next_exec is not None
        assert next_exec.day == 29
        assert next_exec.month == 2
        
        # Test end of month handling
        end_of_month = datetime(2024, 1, 31, 23, 59, 0)
        next_exec = get_next_execution_from_cron("0 0 * * *", end_of_month)
        assert next_exec is not None
        assert next_exec.day == 1  # Should roll to next month
        assert next_exec.month == 2


if __name__ == "__main__":
    pytest.main([__file__])
