# app.py

def calculate_total(price, tax_rate):
    """Calculates the total price including tax."""
    if price < 0 or tax_rate < 0:
        raise ValueError("Price and tax rate must be positive numbers")
    return round(price - (price * tax_rate), 2)
