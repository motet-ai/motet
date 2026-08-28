"""
Motet - Cron Utilities

Copyright (c) 2024-2025 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-06-18

Description:
    Comprehensive cron utilities for the Motet distributed framework.
    Provides cron expression validation, next execution time calculation,
    and human-readable cron descriptions. Includes error handling and
    comprehensive logging for schedule management.

Dependencies:
    - datetime: Time and date handling for cron calculations
    - croniter: Cron expression parsing and calculation
    - structlog: Structured logging
    - typing: Type hints and annotations

Usage:
    from motet.core.orchestration.scheduling.cron_utils import (
        validate_cron_expression, get_next_execution_from_cron, describe_cron_expression
    )
    
    # Validate cron expression
    is_valid = validate_cron_expression("0 9 * * MON-FRI")
    
    # Get next execution time
    next_time = get_next_execution_from_cron("0 9 * * MON-FRI")
    
    # Get human-readable description
    description = describe_cron_expression("0 9 * * MON-FRI")

Notes:
    - Provides cron expression validation and error handling
    - Includes next execution time calculation with timezone support
    - Supports human-readable cron descriptions
    - Includes comprehensive error handling and logging
    - Supports various cron expression formats and patterns
    - Integrates with distributed scheduling system
    - Includes timezone-aware execution time calculations
"""


from datetime import datetime
from typing import Optional

import structlog
from croniter import croniter

logger = structlog.get_logger(__name__)


class CronExpressionError(Exception):
    """Exception raised for invalid cron expressions"""
    pass


def validate_cron_expression(cron_expression: str) -> bool:
    """
    Validate a cron expression.
    
    Args:
        cron_expression: The cron expression to validate (e.g., "0 9 * * MON-FRI")
        
    Returns:
        True if the cron expression is valid, False otherwise
    """
    try:
        # Try to create a croniter instance to validate the expression
        croniter(cron_expression)
        return True
    except Exception as e:
        logger.debug("Invalid cron expression",
                    cron_expression=cron_expression,
                    error=str(e))
        return False


def get_next_execution_from_cron(cron_expression: str, 
                                base_time: Optional[datetime] = None) -> Optional[datetime]:
    """
    Calculate the next execution time from a cron expression.
    
    Args:
        cron_expression: The cron expression (e.g., "0 9 * * MON-FRI")
        base_time: The base time to calculate from (defaults to current UTC time)
        
    Returns:
        The next execution datetime, or None if the cron expression is invalid
        
    Raises:
        CronExpressionError: If the cron expression is invalid
    """
    try:
        if base_time is None:
            base_time = datetime.utcnow()
            
        # Create croniter instance
        cron = croniter(cron_expression, base_time)
        
        # Get the next execution time
        next_execution = cron.get_next(datetime)
        
        logger.debug("Calculated next execution from cron",
                    cron_expression=cron_expression,
                    base_time=base_time.isoformat(),
                    next_execution=next_execution.isoformat())
        
        return next_execution
        
    except Exception as e:
        logger.error("Failed to calculate next execution from cron",
                    cron_expression=cron_expression,
                    base_time=base_time.isoformat() if base_time else None,
                    error=str(e), exc_info=True)
        raise CronExpressionError(f"Invalid cron expression '{cron_expression}': {e}")


def get_previous_execution_from_cron(cron_expression: str,
                                   base_time: Optional[datetime] = None) -> Optional[datetime]:
    """
    Calculate the previous execution time from a cron expression.
    
    Args:
        cron_expression: The cron expression (e.g., "0 9 * * MON-FRI")
        base_time: The base time to calculate from (defaults to current UTC time)
        
    Returns:
        The previous execution datetime, or None if the cron expression is invalid
        
    Raises:
        CronExpressionError: If the cron expression is invalid
    """
    try:
        if base_time is None:
            base_time = datetime.utcnow()
            
        # Create croniter instance
        cron = croniter(cron_expression, base_time)
        
        # Get the previous execution time
        previous_execution = cron.get_prev(datetime)
        
        logger.debug("Calculated previous execution from cron",
                    cron_expression=cron_expression,
                    base_time=base_time.isoformat(),
                    previous_execution=previous_execution.isoformat())
        
        return previous_execution
        
    except Exception as e:
        logger.error("Failed to calculate previous execution from cron",
                    cron_expression=cron_expression,
                    base_time=base_time.isoformat() if base_time else None,
                    error=str(e), exc_info=True)
        raise CronExpressionError(f"Invalid cron expression '{cron_expression}': {e}")


def describe_cron_expression(cron_expression: str) -> str:
    """
    Get a human-readable description of a cron expression.
    
    Args:
        cron_expression: The cron expression to describe
        
    Returns:
        A human-readable description of the cron expression
    """
    try:
        # Validate the expression first
        if not validate_cron_expression(cron_expression):
            return f"Invalid cron expression: {cron_expression}"
        
        # Common cron patterns and their descriptions
        descriptions = {
            "* * * * *": "Every minute",
            "0 * * * *": "Every hour",
            "0 0 * * *": "Every day at midnight",
            "0 0 * * 0": "Every Sunday at midnight",
            "0 0 1 * *": "First day of every month at midnight",
            "0 9 * * MON-FRI": "Weekdays at 9:00 AM",
            "0 9 * * 1-5": "Weekdays at 9:00 AM",
            "*/5 * * * *": "Every 5 minutes",
            "*/10 * * * *": "Every 10 minutes",
            "*/15 * * * *": "Every 15 minutes",
            "*/30 * * * *": "Every 30 minutes",
            "0 */2 * * *": "Every 2 hours",
            "0 */6 * * *": "Every 6 hours",
            "0 */12 * * *": "Every 12 hours",
        }
        
        # Check for exact matches first
        if cron_expression in descriptions:
            return descriptions[cron_expression]
        
        # For other expressions, provide a basic description
        parts = cron_expression.split()
        if len(parts) == 5:
            minute, hour, day, month, weekday = parts
            
            desc_parts = []
            
            # Minute
            if minute == "*":
                desc_parts.append("every minute")
            elif minute.startswith("*/"):
                desc_parts.append(f"every {minute[2:]} minutes")
            elif minute == "0":
                desc_parts.append("at minute 0")
            else:
                desc_parts.append(f"at minute {minute}")
            
            # Hour
            if hour != "*":
                if hour.startswith("*/"):
                    desc_parts.append(f"every {hour[2:]} hours")
                else:
                    desc_parts.append(f"at hour {hour}")
            
            # Day
            if day != "*":
                desc_parts.append(f"on day {day}")
            
            # Month
            if month != "*":
                desc_parts.append(f"in month {month}")
            
            # Weekday
            if weekday != "*":
                weekday_names = {
                    "0": "Sunday", "1": "Monday", "2": "Tuesday", 
                    "3": "Wednesday", "4": "Thursday", "5": "Friday", "6": "Saturday",
                    "7": "Sunday"  # Alternative Sunday representation
                }
                if weekday in weekday_names:
                    desc_parts.append(f"on {weekday_names[weekday]}")
                elif weekday == "MON-FRI" or weekday == "1-5":
                    desc_parts.append("on weekdays")
                else:
                    desc_parts.append(f"on weekday {weekday}")
            
            return " ".join(desc_parts).capitalize()
        
        return f"Cron expression: {cron_expression}"
        
    except Exception as e:
        logger.debug("Failed to describe cron expression",
                    cron_expression=cron_expression,
                    error=str(e))
        return f"Cron expression: {cron_expression}"


def is_time_for_cron_execution(cron_expression: str,
                              last_execution: Optional[datetime] = None,
                              current_time: Optional[datetime] = None) -> bool:
    """
    Check if it's time to execute based on a cron expression.
    
    Args:
        cron_expression: The cron expression
        last_execution: The last execution time (optional)
        current_time: The current time to check against (defaults to UTC now)
        
    Returns:
        True if it's time to execute, False otherwise
    """
    try:
        if current_time is None:
            current_time = datetime.utcnow()
        
        # If no last execution, check if we should execute now
        if last_execution is None:
            # Get the previous scheduled time from cron
            prev_execution = get_previous_execution_from_cron(cron_expression, current_time)
            # If the previous scheduled time was recent (within the last minute),
            # we should execute now
            if prev_execution and (current_time - prev_execution).total_seconds() < 60:
                return True
            return False
        
        # Get the next execution time after the last execution
        next_execution = get_next_execution_from_cron(cron_expression, last_execution)
        
        # Check if the current time has passed the next execution time
        return next_execution is not None and current_time >= next_execution
        
    except Exception as e:
        logger.error("Failed to check cron execution time",
                    cron_expression=cron_expression,
                    last_execution=last_execution.isoformat() if last_execution else None,
                    current_time=current_time.isoformat() if current_time else None,
                    error=str(e), exc_info=True)
        return False
