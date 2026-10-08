import os
import httpx
import sys

BASE_URL = "http://127.0.0.1:8000"

def test_workflow():
    print("Starting Payroll E2E Workflow verification...")
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)

    # 1. Login as Maker
    print("\n1. Logging in as Maker (tanya.agrawal@1mg.com)...")
    res = client.post("/api/auth/sso-mock", json={
        "email": "tanya.agrawal@1mg.com"
    })
    if res.status_code != 200:
        print(f"FAILED: Maker login failed ({res.status_code}): {res.text}")
        sys.exit(1)
    
    print("SUCCESS: Maker logged in successfully.")
    
    # Check config
    res = client.get("/api/workflow/config")
    if res.status_code != 200:
        print(f"FAILED: GET /api/workflow/config failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Maker loaded config successfully.")

    # 2. Save Maker sheet
    print("\n2. Saving draft payroll sheet as Maker...")
    rows = [
        {
            "empCode": "EMP101",
            "empName": "Karan Johar",
            "grade": "M1",
            "designation": "Consultant",
            "amount": "5000",
            "overtimeHours": "10",
            "remarks": "PSP overtime July",
            "flaggedColumns": [],
            "history": []
        }
    ]
    res = client.post(
        "/api/workflow/save-maker",
        params={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"},
        json=rows
    )
    if res.status_code != 200:
        print(f"FAILED: save-maker failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Draft saved successfully.")

    # 3. Submit sheet to HRBP
    print("\n3. Submitting sheet to HRBP...")
    res = client.post(
        "/api/workflow/submit-hrbp",
        params={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"}
    )
    if res.status_code != 200:
        print(f"FAILED: submit-hrbp failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Sheet submitted to HRBP.")

    # Logout maker by resetting client headers/cookies
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)

    # 4. Login as HRBP
    print("\n4. Logging in as HRBP (charvi.sarin@1mg.com)...")
    res = client.post("/api/auth/sso-mock", json={
        "email": "charvi.sarin@1mg.com"
    })
    if res.status_code != 200:
        print(f"FAILED: HRBP login failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: HRBP logged in successfully.")

    # Get pending HRBP sheets
    res = client.get(
        "/api/workflow/hrbp",
        params={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"}
    )
    if res.status_code != 200 or len(res.json()) == 0:
        print(f"FAILED: HRBP did not see pending sheet ({res.status_code}): {res.text}")
        sys.exit(1)
    print(f"SUCCESS: HRBP retrieved pending sheet. Count: {len(res.json())}")

    # Save HRBP Review comments
    print("\n5. Saving HRBP review remarks...")
    res = client.post(
        "/api/workflow/save-hrbp-review",
        json={
            "comments": "Looks good from HRBP",
            "flaggedColumns": [],
            "module": "OVERTIME HOURS",
            "employeeHome": "Dataverse"
        }
    )
    if res.status_code != 200:
        print(f"FAILED: save-hrbp-review failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: HRBP review remarks saved.")

    # Forward to HOD
    print("\n6. Forwarding sheet to HOD...")
    res = client.post(
        "/api/workflow/submit-hod",
        params={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"}
    )
    if res.status_code != 200:
        print(f"FAILED: submit-hod failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Sheet forwarded to HOD.")

    # Reset client
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)

    # 7. Login as HOD
    print("\n7. Logging in as HOD (nikhil.doegar@1mg.com)...")
    res = client.post("/api/auth/sso-mock", json={
        "email": "nikhil.doegar@1mg.com"
    })
    if res.status_code != 200:
        print(f"FAILED: HOD login failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: HOD logged in successfully.")

    # Get pending HOD sheets
    res = client.get(
        "/api/workflow/hod",
        params={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"}
    )
    if res.status_code != 200 or len(res.json()) == 0:
        print(f"FAILED: HOD did not see pending sheet ({res.status_code}): {res.text}")
        sys.exit(1)
    print(f"SUCCESS: HOD retrieved pending sheet. Count: {len(res.json())}")

    # Save HOD approval comments
    print("\n8. Saving HOD approval remarks...")
    res = client.post(
        "/api/workflow/save-hod-review",
        json={
            "comments": "Approved by HOD",
            "module": "OVERTIME HOURS",
            "employeeHome": "Dataverse"
        }
    )
    if res.status_code != 200:
        print(f"FAILED: save-hod-review failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: HOD approval remarks saved.")

    # Release to Payroll Admin
    print("\n9. Releasing sheet to Payroll...")
    res = client.post(
        "/api/workflow/submit-payroll",
        params={"module": "OVERTIME HOURS", "employeeHome": "Dataverse"}
    )
    if res.status_code != 200:
        print(f"FAILED: submit-payroll failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Sheet released to Payroll.")

    # Reset client
    client = httpx.Client(base_url=BASE_URL, timeout=10.0)

    # 10. Login as Payroll Admin
    print("\n10. Logging in as Payroll Admin (payroll@1mg.com)...")
    res = client.post("/api/auth/sso-mock", json={
        "email": "payroll@1mg.com"
    })
    if res.status_code != 200:
        print(f"FAILED: Payroll Admin login failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Payroll Admin logged in successfully.")

    # Get payroll queue
    res = client.get("/api/workflow/payroll")
    if res.status_code != 200 or len(res.json()) == 0:
        print(f"FAILED: Payroll Admin did not see pending queue ({res.status_code}): {res.text}")
        sys.exit(1)
    print(f"SUCCESS: Payroll Admin retrieved pending queue. Count: {len(res.json())}")

    # Close payroll
    print("\n11. Finalizing and closing payroll sheet...")
    res = client.post("/api/workflow/payroll/close", json={"module": "OVERTIME HOURS"})
    if res.status_code != 200:
        print(f"FAILED: payroll/close failed ({res.status_code}): {res.text}")
        sys.exit(1)
    print("SUCCESS: Payroll closed and archived.")

    # Check closed sheets
    print("\n12. Verifying closed sheet archive...")
    res = client.get("/api/workflow/closed")
    if res.status_code != 200 or len(res.json().get("months", [])) == 0:
        print(f"FAILED: closed sheet not archived ({res.status_code}): {res.text}")
        sys.exit(1)
    
    print("\nALL WORKFLOW E2E TESTS PASSED SUCCESSFULLY! 100% CORRECT!")

if __name__ == "__main__":
    test_workflow()
