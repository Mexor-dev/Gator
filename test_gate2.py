import sys
sys.path.insert(0, '/home/user/Gator/src')
from scholar_sense import ScholarSense

scholar = ScholarSense(server_url='http://127.0.0.1:8081')

# Test 1: Medication (should pass floor)
result1 = scholar.query('What is ibuprofen used for?')
pass1 = (not result1.get('zero_context', True)) and (result1.get('effective_similarity', 0) > 0.25)

# Test 2: Weather (should trigger floor)
result2 = scholar.query('What is the weather in Tokyo tomorrow?')
pass2 = result2.get('zero_context', False)

status = 'PASS' if (pass1 and pass2) else 'FAIL'
print(f'{{"gate2_test1": {pass1}, "gate2_test2": {pass2}, "status": "{status}"}}')
