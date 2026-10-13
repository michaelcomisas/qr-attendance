from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    class_name = db.Column(db.String(50))
    qr_code_data = db.Column(db.String(200), unique=True)

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.String(20), db.ForeignKey('student.student_id'), nullable=False)
    class_name = db.Column(db.String(50))
    date = db.Column(db.Date, default=lambda: datetime.utcnow().date())   # ✅ callable
    time_in = db.Column(db.Time, default=lambda: datetime.utcnow().time()) # ✅ callable
    status = db.Column(db.String(20), default='Present')