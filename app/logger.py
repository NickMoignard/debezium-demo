"""
Logging utility for the data generator.
Provides a configured logger instance with proper formatting and output handling.
"""
import logging
import sys


def get_logger(name: str = "data-generator") -> logging.Logger:
    """
    Get or create a logger instance with standardized configuration.
    
    Args:
        name: Logger name (default: "data-generator")
    
    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    
    # Only configure if handlers haven't been added yet
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        
        # Create console handler with formatting
        handler = logging.StreamHandler(sys.stdout)
        handler.setLevel(logging.INFO)
        
        # Force unbuffered output for Docker logs
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(line_buffering=True)
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(line_buffering=True)
        
        # Format: [timestamp] LEVEL - message
        formatter = logging.Formatter(
            fmt='[%(asctime)s] %(levelname)s - %(message)s',
            datefmt='%Y-%m-%dT%H:%M:%S'
        )
        handler.setFormatter(formatter)
        
        logger.addHandler(handler)
    
    return logger
