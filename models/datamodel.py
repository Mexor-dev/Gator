"""Core data model."""

class DataModel:
    """Represents application data."""
    
    def __init__(self, name="default"):
        self.name = name
    
    def serialize(self):
        return {"name": self.name}
