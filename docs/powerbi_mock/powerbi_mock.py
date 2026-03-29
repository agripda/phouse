import sqlite3, csv
conn = sqlite3.connect('data/leave.db')
cursor = conn.execute('SELECT * FROM LeaveDay')
with open('leave_day.csv', 'w', newline='') as f:
    w = csv.writer(f)
    w.writerow([d[0] for d in cursor.description])
    w.writerows(cursor.fetchall())
print('done')