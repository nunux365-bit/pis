/**
 * Shared types for all payroll workflow pages
 * (maker, hrbp, hod, ops).
 */

/** An earning head / pay component returned by /api/workflow/config */
export type EarningHead = {
  name: string;
  allowed_homes?: string[];
  employee_home?: string;
  hrbp?: string | string[];
};

/**
 * A single payroll row as returned by the workflow API.
 * Fields that only appear in specific pages are optional.
 */
export type PayrollRow = {
  id: number;
  /** 1-based serial number populated by the maker page */
  sno?: number;
  empCode: string;
  empName: string;
  grade: string;
  designation: string;
  employeeHome: string;
  type: string;
  module: string;
  amount: string;
  effectiveFrom: string;
  effectiveTo: string;
  overtimeHours: string;
  holidayDate: string;
  remarks: string;
  status?: string;
  /** Full edit history, used by the maker page */
  history?: any[];
  /** Columns flagged for review */
  flaggedColumns?: string[];
  hrbpComments?: string;
  hodComments?: string;
  initiatorEmail?: string;
  /** Ops-page fields set when a module is closed */
  closedAt?: string;
  closedByEmail?: string;
  /** Payment month tag used on the maker page */
  paymentMonth?: string;
};

/** An entry in the workflow audit / history log */
export type AuditItem = {
  id: number;
  action: string;
  user: string;
  timestamp: string;
  remarks?: string;
};