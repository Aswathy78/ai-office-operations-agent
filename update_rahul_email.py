from app.db.database import SessionLocal
from app.meeting.models import Employee

OLD_EMAIL = "ananashraf0506@gmail.com"
NEW_EMAIL = "rahulexample.test@gmail.com"

db = SessionLocal()

try:
    employee = db.query(Employee).filter(
        Employee.email == OLD_EMAIL
    ).first()

    if employee:
        employee.email = NEW_EMAIL
        db.commit()
        print("Email updated successfully!")
        print(f"Name: {employee.name}")
        print(f"New email: {employee.email}")
    else:
        print("Employee not found. No changes made.")

finally:
    db.close()