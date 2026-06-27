/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

export class AppDB {
  private dbName = "LinhHoatDB";
  private dbVersion = 1;
  private db: IDBDatabase | null = null;
  private useMemory = false;
  private memoryCache: Record<string, any> = {};

  constructor() {
    if (typeof indexedDB === 'undefined') {
      this.useMemory = true;
    }
  }

  private init(): Promise<IDBDatabase> {
    if (this.db) return Promise.resolve(this.db);
    return new Promise((resolve, reject) => {
      try {
        const request = indexedDB.open(this.dbName, this.dbVersion);
        request.onerror = () => {
          console.warn("IndexedDB open failed, falling back to localStorage/memory");
          this.useMemory = true;
          reject(request.error);
        };
        request.onsuccess = () => {
          this.db = request.result;
          resolve(request.result);
        };
        request.onupgradeneeded = () => {
          const db = request.result;
          if (!db.objectStoreNames.contains("state")) {
            db.createObjectStore("state");
          }
        };
      } catch (err) {
        console.warn("IndexedDB error, falling back to memory/localStorage", err);
        this.useMemory = true;
        reject(err);
      }
    });
  }

  async get(key: string): Promise<any> {
    if (this.useMemory) {
      const val = this.memoryCache[key];
      if (val !== undefined) return val;
      try {
        const local = localStorage.getItem(key);
        return local ? JSON.parse(local) : null;
      } catch (e) {
        return null;
      }
    }

    try {
      const db = await this.init();
      return new Promise((resolve) => {
        const transaction = db.transaction("state", "readonly");
        const store = transaction.objectStore("state");
        const request = store.get(key);
        request.onerror = () => resolve(this.getFallback(key));
        request.onsuccess = () => resolve(request.result === undefined ? this.getFallback(key) : request.result);
      });
    } catch (e) {
      return this.getFallback(key);
    }
  }

  private getFallback(key: string): any {
    const val = this.memoryCache[key];
    if (val !== undefined) return val;
    try {
      const local = localStorage.getItem(key);
      return local ? JSON.parse(local) : null;
    } catch (e) {
      return null;
    }
  }

  async set(key: string, value: any): Promise<void> {
    this.memoryCache[key] = value;
    
    // Attempt to sync to localStorage fallback for persistent simple states
    try {
      const stringified = JSON.stringify(value);
      if (stringified.length < 500000) { 
        localStorage.setItem(key, stringified);
      }
    } catch (e) {}

    if (this.useMemory) return;

    try {
      const db = await this.init();
      return new Promise<void>((resolve) => {
        const transaction = db.transaction("state", "readwrite");
        const store = transaction.objectStore("state");
        const request = store.put(value, key);
        request.onerror = () => {
          console.warn("IndexedDB put failed");
          resolve();
        };
        request.onsuccess = () => resolve();
      });
    } catch (e) {
      console.warn("IndexedDB set error", e);
    }
  }
}

export const dbStore = new AppDB();
