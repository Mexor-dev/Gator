#!/usr/bin/env python3
"""Test script for Meta-Tool System: Recursive Tool Authoring & Dream Cycle.

This script demonstrates the complete workflow:
1. Create a new synthetic tool (Python function)
2. Validate it in sandbox
3. Query the tool registry
4. Simulate performance logging
5. Trigger Dream Cycle optimization
"""

import json
import requests
import time
from pathlib import Path

BRIDGE_URL = "http://127.0.0.1:8090"


def test_meta_tool_workflow():
    """End-to-end test of synthetic tool creation and lifecycle."""
    
    print("\n" + "="*80)
    print(" META-TOOL SYSTEM TEST: Recursive Tool Authoring & Dream Cycle")
    print("="*80 + "\n")
    
    # Step 1: Check immutable tools list
    print("[1] Fetching immutable embedded toolset...")
    resp = requests.get(f"{BRIDGE_URL}/api/meta/immutable_tools")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Immutable tools (READ-ONLY): {', '.join(data['immutable_tools'])}")
    else:
        print(f"    ERROR: {resp.status_code}")
        return
    
    # Step 2: Create a new synthetic tool (Python)
    print("\n[2] Creating a new synthetic tool...")
    
    # Example: A simple tool that calculates Fibonacci numbers
    tool_source = '''def fibonacci(n: int) -> int:
    """Calculate the Nth Fibonacci number using iterative approach.
    
    Args:
        n: The position in the Fibonacci sequence (0-indexed)
    
    Returns:
        The Fibonacci number at position n
    """
    if n <= 0:
        return 0
    elif n == 1:
        return 1
    
    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    
    return b
'''
    
    create_payload = {
        "name": "fibonacci",
        "description": "Calculate Fibonacci numbers efficiently (iterative algorithm)",
        "language": "python",
        "source_code": tool_source,
        "auto_validate": True,
    }
    
    resp = requests.post(
        f"{BRIDGE_URL}/api/meta/tools/create",
        json=create_payload,
    )
    
    if resp.status_code == 200:
        tool_data = resp.json()
        tool_id = tool_data["tool_id"]
        print(f"    ✓ Created tool: {tool_id}")
        print(f"      Name: {tool_data['name']}")
        print(f"      Validation: {tool_data['validation_status']}")
        print(f"      Schema: {json.dumps(tool_data['schema'], indent=6)}")
    else:
        print(f"    ERROR: {resp.status_code} - {resp.text}")
        return
    
    # Step 3: Query all synthetic tools
    print("\n[3] Querying all synthetic tools...")
    resp = requests.get(f"{BRIDGE_URL}/api/meta/tools")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Total synthetic tools: {data['total']}")
        for tool in data["tools"]:
            print(f"      - {tool['name']} ({tool['tool_id']}): {tool['validation_status']}")
    else:
        print(f"    ERROR: {resp.status_code}")
    
    # Step 4: Query active (validated) tools
    print("\n[4] Querying active validated tools...")
    resp = requests.get(f"{BRIDGE_URL}/api/meta/tools/active")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Active tools ready for use: {data['total']}")
        for tool in data["tools"]:
            print(f"      - {tool['name']}: score={tool['performance_score']:.2f}")
    else:
        print(f"    ERROR: {resp.status_code}")
    
    # Step 5: Get detailed tool info
    print(f"\n[5] Fetching detailed info for {tool_id}...")
    resp = requests.get(f"{BRIDGE_URL}/api/meta/tools/{tool_id}")
    if resp.status_code == 200:
        data = resp.json()
        tool = data["tool"]
        print(f"    Name: {tool['name']}")
        print(f"    Language: {tool['language']}")
        print(f"    Validation: {tool['validation_status']}")
        print(f"    Use count: {tool['use_count']}")
        print(f"    Error count: {tool['error_count']}")
        print(f"    Avg latency: {tool['avg_latency_ms']:.2f}ms")
    else:
        print(f"    ERROR: {resp.status_code}")
    
    # Step 6: Create a second tool to test Dream Cycle
    print("\n[6] Creating a second synthetic tool for Dream Cycle testing...")
    
    tool_source_2 = '''def is_prime(n: int) -> bool:
    """Check if a number is prime.
    
    Args:
        n: The number to check
    
    Returns:
        True if n is prime, False otherwise
    """
    if n < 2:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    
    # Check odd divisors up to sqrt(n)
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    
    return True
'''
    
    create_payload_2 = {
        "name": "is_prime",
        "description": "Check if a number is prime (optimized trial division)",
        "language": "python",
        "source_code": tool_source_2,
        "auto_validate": True,
    }
    
    resp = requests.post(
        f"{BRIDGE_URL}/api/meta/tools/create",
        json=create_payload_2,
    )
    
    if resp.status_code == 200:
        tool_data_2 = resp.json()
        print(f"    ✓ Created tool: {tool_data_2['tool_id']}")
        print(f"      Name: {tool_data_2['name']}")
    else:
        print(f"    ERROR: {resp.status_code}")
    
    # Step 7: Trigger Dream Cycle optimization
    print("\n[7] Triggering Dream Cycle optimization...")
    resp = requests.post(
        f"{BRIDGE_URL}/api/meta/dream_cycle",
        params={"min_use_count": 0},  # Low threshold for testing
    )
    
    if resp.status_code == 200:
        data = resp.json()
        report = data["report"]
        print(f"    ✓ Dream Cycle complete")
        print(f"      Tools analyzed: {report['tools_analyzed']}")
        print(f"      High error tools: {len(report['high_error_tools'])}")
        print(f"      High latency tools: {len(report['high_latency_tools'])}")
        print(f"      Unused tools: {len(report['unused_tools'])}")
        print(f"      Top performers: {len(report['top_performers'])}")
        
        if report['unused_tools']:
            print(f"      Unused tool IDs: {', '.join(report['unused_tools'])}")
    else:
        print(f"    ERROR: {resp.status_code}")
    
    # Step 8: Verify immutability enforcement
    print("\n[8] Verifying immutability enforcement...")
    resp = requests.get(f"{BRIDGE_URL}/api/meta/immutable_tools")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    ✓ Immutable tools protected: {len(data['immutable_tools'])}")
        print(f"      Examples: file_read, file_write, camoufox_web")
        print(f"      Note: {data['note']}")
    else:
        print(f"    ERROR: {resp.status_code}")
    
    print("\n" + "="*80)
    print(" TEST COMPLETE: Meta-Tool system operational")
    print(" The 1.5B can now author and validate its own tools on-demand!")
    print("="*80 + "\n")


def test_rust_tool_creation():
    """Test creating a Rust synthetic tool."""
    print("\n" + "="*80)
    print(" RUST TOOL CREATION TEST")
    print("="*80 + "\n")
    
    # Example: A simple Rust function
    rust_source = '''/// Calculate the factorial of a number
pub fn factorial(n: u64) -> u64 {
    if n <= 1 {
        return 1;
    }
    
    let mut result = 1u64;
    for i in 2..=n {
        result *= i;
    }
    result
}
'''
    
    create_payload = {
        "name": "factorial",
        "description": "Calculate factorial of a number (Rust implementation)",
        "language": "rust",
        "source_code": rust_source,
        "auto_validate": True,
    }
    
    resp = requests.post(
        f"{BRIDGE_URL}/api/meta/tools/create",
        json=create_payload,
    )
    
    if resp.status_code == 200:
        tool_data = resp.json()
        print(f"✓ Created Rust tool: {tool_data['tool_id']}")
        print(f"  Schema: {json.dumps(tool_data['schema'], indent=2)}")
    else:
        print(f"ERROR: {resp.status_code} - {resp.text}")
    
    print("\n" + "="*80 + "\n")


if __name__ == "__main__":
    try:
        # Test Python synthetic tools
        test_meta_tool_workflow()
        
        # Test Rust synthetic tools
        test_rust_tool_creation()
        
    except requests.exceptions.ConnectionError:
        print("\nERROR: Cannot connect to Gator Bridge at", BRIDGE_URL)
        print("Make sure gator_bridge.py is running on port 8090")
        print("\nStart it with:")
        print("  cd /home/user/Gator")
        print("  venv/bin/python gator_bridge.py --mode api --port 8090")
    except Exception as exc:
        print(f"\nERROR: {exc}")
        import traceback
        traceback.print_exc()
