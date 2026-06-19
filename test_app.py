# test_app.py
import unittest
from app import calculate_total

class TestApp(unittest.TestCase):
    
    def test_correct_calculation(self):
        # $100 item + 10% tax should equal $110
        self.assertEqual(calculate_total(100, 0.10), 110.00)
        
    def test_negative_values(self):
        # Ensure it throws an error if someone inputs negative numbers
        with self.assertRaises(ValueError):
            calculate_total(-50, 0.05)

if __name__ == '__main__':
    unittest.main()
