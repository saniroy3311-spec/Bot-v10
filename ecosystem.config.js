module.exports = {
  apps: [
    {
      name: "client-dashboard",
      cwd: "/root/Bot-v10",
      script: "uvicorn",
      args: "dashboard.main:app --host 0.0.0.0 --port 8082",
      interpreter: "python3",
      env: {
        TURSO_URL: "libsql://bot-v10-saniroy.aws-ap-south-1.turso.io",
        TURSO_TOKEN: "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3ODE0MzQ2MzcsImlkIjoiMDE5ZWM1YzYtNTcwMS03NzBiLWJhYTQtNTQ0ZDZhN2JkNGViIiwicmlkIjoiOWI3ZWU4ZDMtM2U2YS00MzIxLWFlNjItZDI5N2JiYmQwYmU4In0.xHTGR5EoRWaGoDgpEQ_F9nYKvlIZMZMCy1DIvr3opZWHkSfWZDB-pgSk-bDG3w85vwox2zkir9X-sXO0o9tTDA",
        DASHBOARD_USER: "admin",
        DASHBOARD_PASS: "Sani@3010"
      }
    }
  ]
}
