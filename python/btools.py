################################################################
#  Module: btools.py
#  Desc  : Provides methods for handling pribbon matrix
#          operations in parallel
################################################################
from   netCDF4 import Dataset
from   mpi4py import MPI
import numpy as np
import array
import math
import sys
import os


class BTools:

    ################################################################
    #  Method: __init__
    #  Desc  : Constructor
    #  Args  : comm    (in): communicator
    #          mpiftype(in): MPI float type of data 
    #          nens    (in): number of ensembles 
    #          gn      (in): 1d array of global data sizes: (Nz, Ny, Nz)
    #          debug   (in): print debug info (1); else don't (0)
    # Returns: none
    ################################################################
    def __init__(self, comm, mpiftype, nens, gn, debug=False):

        # Class member data:
        self.comm_      = comm
        self.myrank_    = comm.Get_rank()
        self.nprocs_    = comm.Get_size()
        self.mpiftype_  = mpiftype

        self.send_type_ = mpiftype
        self.recv_type_ = []
        assert len(gn)==3, "Invalid dimension spec"
        self.nens_      = nens
        self.gn_        = gn

        self.debug_     = debug

        # Create recv buffs for this task:
        nxmax = 0
        for i in range(0,self.nprocs_):
            (ib, ie) = BTools.range(self.gn_[2], self.nprocs_, i)
            nxmax = max(nxmax, ie-ib+1)
        
        self.nxmax_ = nxmax
        szbuff = nens*nxmax*gn[0]*gn[1]

        if self.debug_:
          print(self.myrank_, ": __init__: nxmax=",nxmax," szbuff=",szbuff," gn=",gn)
          sys.stdout.flush()
        buffdims = ([self.comm_.Get_size(), szbuff])
        if   mpiftype == MPI.FLOAT:
            self.recvbuff_ = np.ndarray(buffdims, dtype='f')
        elif mpiftype == MPI.DOUBLE:
            self.recvbuff_ = np.ndarray(buffdims, dtype='d')
        else:
            assert 0, "Input type must be float or double"
        
        self.recvbuff_.fill(self.myrank_)

        linsz = szbuff**2

        if self.debug_:
          print("BTools::__init__: linsz=",linsz)
          sys.stdout.flush()
        if   mpiftype == MPI.FLOAT:
            self.Bp_ = np.zeros(linsz, dtype=np.float32)
        elif mpiftype == MPI.DOUBLE:
            self.Bp_ = np.zeros(linsz, dtype=np.float64)
        else:
            assert 0, "Input type must be float or double"
        self.Ip_ = np.zeros(linsz, dtype=np.int64)
        self.Jp_ = np.zeros(linsz, dtype=np.int64)

	# end, constructor


    ################################################################
    #  Method: range
    #  Desc  : Compute (Fortran) array bounds given global length
    #  Args  : 
    #          gn     (in): global length 
    #          nprocs (in): total number of tasks
    #          myrank (in): task's rank
    # Returns: (ib ie): task's local starting, ending indices
    ################################################################
    @staticmethod
    def range(gn, nprocs, myrank):

        gib = 1
        gie = gn

        i1 = 0
        i2 = 0
        ib = 0
        ie = 0

        i1 = int((gie - gib + 1) / nprocs)
        i2 = (gie - gib + 1) % nprocs
               
        ib = myrank*i1 + gib + min(myrank, i2)
        ie = ib + i1 - 1
        if i2 > myrank: 
           ie = ie + 1
	
        return int(ib-1), int(ie-1)  # end, range method
	

    ################################################################
    #  Method: trans_type
    #  Desc  : Create MPI data type for doing transfer
    #          sends & receives
    #  Args  : imin  (in), 
    #          imax  (in): max & min of 1st dimension
    #          jmin  (in), 
    #          jmax  (in): max & min of 2nd dimension
    #          kmin  (in), 
    #          kmax  (in): max & min of 3rd dimension
    #          ib    (in): local starting index of 1st dimension
    #          ie    (in): local ending index of 1st dimension
    #          itype (in): input datatype
    #          otype(out): new output datatype
    # Returns: none
    ################################################################
    @staticmethod
    def trans_type(imin, imax, jmin, jmax, kmin, kmax,  \
                   ib, ie, itype, otype):
        
        extent = itype.get_extent()       
             
        ilen = ib   - ie   + 1  
        jlen = jmax - jmin + 1  
        klen = kmax - kmin + 1  
        
        sizes    = (imax-imin+1, jmax-jmin+1, kmax-kmin+1)
        subsizes = (ilen, jlen, klen)
        idisp    = (ib  , jmin, kmin)
        otype    = itype.create_subarray(sizes, subsizes, idist, order = MPI.ORDER_FORTRAN)
        otype.Commit()

        return # end, trans_type method
	

    ################################################################
    #  Method: buildB
    #  Desc  : Create 'B-matrix' from distributed data.
    #          Note: This method currently uses an MPI collective
    #                to gather all task-'local' data into the recvbuff_.
    #                This is equivalent to having each taks read the 
    #                entire grid data independently. So, strictly
    #                speaking, MPI gather isn't required. We retain
    #                it because it's an artifact of attempts to
    #                use point-to-point MOPI calls to sendrrecv data,
    #                and we may wish to return to this, so as to reduce
    #                the memory footprint on each MPI task. This method
    #                computes entries for the _entire_ covariance matrix,
    #                even though it is symmetric. In this way, the distributed
    #                computation is better load-balenced.
    #               
    #  Args  : ldata   : this task's (local)_ data
    #          cthresh : corr coeff threshold
    # Returns: number entries meeting thrershold criterion
    #          B       : array of covariances whose corr. coeffs exceed cthresh
    #          I, J    : arrays of indices into global B mat
    #		             where cov > thresh. Each is of the same length
    ################################################################
    def buildB(self, ldata, cthresh):

        #
        # Build the distributed B matrix.
        #
        B = np.array([])
        I = np.array([])
        J = np.array([])
 	
        if self.debug_:
          print(self.myrank_, ": BTools::buildB: starting...")
          sys.stdout.flush()

	# Gather all slabs here to perform thresholding:
        if self.debug_:
          print(self.myrank_, ": BTools::buildB: ldata.shape=",ldata.shape, " recvbuff.shape=", self.recvbuff_.shape)
          sys.stdout.flush()

        self.comm_.barrier()
        self.comm_.Allgather(ldata,self.recvbuff_)
        self.comm_.barrier()

        if self.debug_:
          print(self.myrank_, ": BTools::buildB: Allgather done")
          sys.stdout.flush()

        # Multiply local data by all gathered data and threshold:
        ntot = 0
        for i in range(0, self.nprocs_):

            n = self.do_thresh(ldata, self.recvbuff_[i,:], i, self.nens_, cthresh, self.Bp_, self.Ip_, self.Jp_) 
      
            if self.debug_:
              print(self.myrank_, ": BTools::buildB: local factor=", ldata)
              print(self.myrank_, ": BTools::buildB: recvbuff[",i,"]=",self.recvbuff_[i,:])
              print(self.myrank_, ": BTools::buildB: I_loc[",i,"]=",self.Ip_)
              print(self.myrank_, ": BTools::buildB: J_loc[",i,"]=",self.Jp_)
              print(self.myrank_, ": BTools::buildB: B_loc[",i,"]=",self.Bp_)
              sys.stdout.flush()

            ntot += n
            # Concatenate new global indices to return arrays:
            B = np.concatenate([B,self.Bp_[0:n]])
            I = np.concatenate([I,self.Ip_[0:n]])
            J = np.concatenate([J,self.Jp_[0:n]])

        if self.debug_:
          print(self.myrank_, ": BTools::buildB: partition thresholding done.")
          sys.stdout.flush()

        return ntot,B,I,J  # end, buildB method
	

    ####################################################
    #  Method: do_thresh
    #  Desc  : With local data, and off-task data, compute
    #          global indices where covariance exceeds
    #          specified threshold.
    #  Args  : ldata : this task's (local) data block, assumed 'flattened'
    #          rdata : off-task (remote) data block, assumed 'flattened'
    #          irecv : task id that rdata is received from
    #          nens  : number of ensembles (always the first index)
    #          thresh: corr coeff threshold
    #          B     : array of covariances whose corr. coeffs exceed cthresh
    #          I, J  : arrays of indices into global B mat
    #		       where cov > thresh. We assume that I, J 
    #                  are the same length as B
    #                  as B
    # Returns: number of values found that meet threshold criterion
    ################################################################
    def do_thresh(self, ldata, rdata, irecv, nens, thresh, B, I, J):
	
        assert len(ldata.shape)==1
        assert len(rdata.shape)==1
        assert len(B)==len(I) and len(B)==len(J)

        
        # Just in case, resize operand arrays if necessary:
        if len(ldata)*len(rdata) > len(self.Bp_):
            if self.mpiftype_ == MPI.FLOAT:
               self.Bp_ = np.zeros(len(ldata)*len(rdata), dtype=np.float32)
            elif self.mpiftype_ == MPI.DOUBLE:
               self.Bp_ = np.zeros(len(ldata)*len(rdata), dtype=np.float64)
            self.Ip_ = np.zeros(len(ldata)*len(rdata), dtype=np.int64) 
            self.Jp_ = np.zeros(len(ldata)*len(rdata), dtype=np.int64)

        imin = 1
        jmin = 1
        kmin = 1
        imax = self.gn_[2]
        jmax = self.gn_[1]
        kmax = self.gn_[0]
                                
        # Find global starting index of recv'd block:
        (ib, ie) = self.range(imax, self.nprocs_, irecv)
        rnb0 = ib*(jmax-jmin+1)*(kmax-kmin+1)
        nrslice = ie - ib + 1
        rdata   = rdata.reshape(nens, self.gn_[0]*self.gn_[1], self.nxmax_)
        if self.debug_:
          print(self.myrank_, ": do_thresh: rdata.shape=",rdata.shape)
          sys.stdout.flush()

        # Find global starting index of local block:
        (ib, ie) = self.range(imax, self.nprocs_, self.myrank_)
        lnb = ib*(jmax-jmin+1)*(kmax-kmin+1)
        nlslice = ie - ib + 1
        if self.debug_:
        # print(self.myrank_, ": do_thresh: ldata=",ldata)
          print(self.myrank_, ": do_thresh: ldata.shape=",ldata.shape, " nlslice=", nlslice)
          sys.stdout.flush()
        ldata   = ldata.reshape(nens, self.gn_[0]*self.gn_[1], nlslice)

    	# Order s.t. we multiply
	    #    Transpose(ldata) X rdata:
        # where Transpose(ldata) is a column vector, 
        # and rdata, a row vector in matrix-speak
        n = 0
        for ii in range(0,nlslice):              # loop over l-slices
          lslice = ldata[:,:,ii]  
          ldims  = lslice.shape
         #if self.debug_:
         #  print(self.myrank_, ": do_thrersh: lslice[",ii,"]=",lslice)
         #  sys.stdout.flush()
          lnb = (ib+ii)*(jmax-jmin+1)*(kmax-kmin+1)
          for i in range(0,ldims[1]):
     	    # Locate in global grid:
            ig    = int( float(lnb+i)/float(self.gn_[0]*self.gn_[1]) )
            ntmp  = ig*self.gn_[0]*self.gn_[1]
            jg    = int( float(lnb+i-ntmp)/float(self.gn_[0]) )
            kg    = lnb + i - jg*self.gn_[0] - ntmp

#           kg    = int( float(lnb+i)/float(self.gn_[1]*self.gn_[2]) )
#           ntmp  = kg*self.gn_[1]*self.gn_[2]
#           jg    = int( float(lnb+i-ntmp)/float(self.gn_[2]) )
#           ig    = lnb + i - jg*self.gn_[2] - ntmp

  	    # Compute global matrix index: 	    
###         Ig    = kg + jg*self.gn_[0] + ig*self.gn_[0]*self.gn_[1]
            Ig    = ig + jg*self.gn_[2] + kg*self.gn_[1]*self.gn_[2]
            CII   = np.mean(lslice[:,i]**2)     # variance=average over ensembles
#           if i%10 == 0:
#             print(self.myrank_, ": do_thrersh: lslice[",i,"]=",lslice[:,i])
            sys.stdout.flush()
            for jj in range(0, nrslice):         # loop over r-slices
              rslice = rdata[:,:,jj]
              rdims  = rslice.shape
              rnb = rnb0 + jj*(jmax-jmin+1)*(kmax-kmin+1)
              for j in range(0,rdims[1]):
                CJJ   = np.mean(rslice[:,j]**2)  # variance=average over ensembles
                vcov  = np.multiply(lslice[:,i], rslice[:,j])
                                                 # covariance for each ensemble member
                covar = np.mean(vcov,0)          # covariance
                ccoef = abs(covar)/math.sqrt(CII*CJJ)  # correlation coefficient
#               if i%10 == 0:
#                 print(self.myrank_,": do_thresh: vcov=", vcov)
#                 print(self.myrank_,": do_thresh: CII=",CII," CJJ=",CJJ, " covar=", covar)
                if ccoef >= thresh:
         	      # Locate in global grid:
                  ig    = int( float(rnb+j)/float(self.gn_[0]*self.gn_[1]) )
                  ntmp  = ig*self.gn_[0]*self.gn_[1]
                  jg    = int( float(rnb+j-ntmp)/float(self.gn_[0]) )
                  kg    = rnb + j - jg*self.gn_[0] - ntmp

#                 kg    = int( float(rnb+j)/float(self.gn_[1]*self.gn_[2]) )
#                 ntmp  = kg*self.gn_[1]*self.gn_[2]
#                 jg    = int( float(rnb+j-ntmp)/float(self.gn_[2]) )
#                 ig    = rnb + j - jg*self.gn_[2] - ntmp
           
    	          # Compute global matrix indices: 	    
###               Jg    = kg + jg*self.gn_[0] + ig*self.gn_[0]*self.gn_[1]
                  Jg    = ig + jg*self.gn_[2] + kg*self.gn_[1]*self.gn_[2]

                  B[n] = covar
                  I[n] = int(Ig)
                  J[n] = int(Jg)
           
                  n += 1

        return n  # end, do_thresh method
	

    ################################################################
    #  Method: getSlabData
    #  Desc  : Reads specified NetCDF4 file, and returns a slab of data
    #          'owned' by specified MPI rank.
    #  Args  : 
    #          fileName    : string, filename of the netCDF file to open
    #          ensembleName: string, name of the ensemble
    #          itime       : integer, single time (for now,later can be ranges)
    #          mpiTasks    : integer, number of MPI tasks (> 0)
    #          mpiRank     : integer, rank of the calling process [0, mpiTasks-1] 
    #                        (zero based)
    #          means       : integer, 1,2,3 where:
    #                         1: <T(x,y,x)> = Sum ens T(ens,x,y,z)/num ensembles
    #                         2: T(ens,x,y,z) - <T(x,y,z)>
    #                         4: raw (no subtracted mean)
    #          decimate    : integer, shorten the slab by decimate (0 is no decimate).
    #                        So, if you decimate by 4, you keep every 4th data point
    # ReturnsL N    : numpy array, data for a particular mpiRank of 
    #                 size (Nx_p, Ny, Nz), where Nx_p is are the number
    #                 x-planes corresponding to mpiRank.
    #          nens : number of ensemble members
    #          gdims: dims of original grid
    ################################################################
    @staticmethod
    def getSlabData(fileName, ensembleName, itime, mpiTasks, mpiRank, means, decimate):
        # N = Btools_getSlabData(fileName, ensembleName, itime, mpiTask, mpiRank, means, decimate)
        # N is a slab of data (x,y,z) 
        #
        # Check the inputs.
        #
        if mpiRank <0 or mpiRank>(mpiTasks-1):
            sys.exit("Error, bad mpiRank in Btools_getSlabData!")

        if (type(mpiRank) is not int):
            sys.exit("Error, bad mpiRank type in Btools_getSlabData!")
   
        if (type(itime) is not int):
            sys.exit("Error, bad itime type in Btools_getSlabData!")

        if (type(fileName) is not str):
            sys.exit("Error, bad fileName type in Btools_getSlabData!")

        decimate = int(decimate)
        if decimate < 0:
          decimate = 1

        nz = 1

        # 
        # Open the netCDF file, what is read depends on means.
        #
#       nc=Dataset(fileName,'r+',parallel=True)
        nc=Dataset(fileName,'r+')
        N = nc.variables[ensembleName]
        if len(N.shape) != 5:
            sys.exit("Error, ensemble should have five dimensions!")
        gdims = N.shape[2:5]

        #
        # Return the selected data.
        #
        #    1: <T(x,y,x)> = Sum ens T(ens,x,y,z)/num ensembles
        #    2: T(ens,x,y,z) - <T(x,y,z)>
        #    3: raw (no subtracted mean)

        if means == 1: # T(x,y,x) >= Sum ens T(ens,x,y,z)/num ensembles
           N = nc.variables[ensembleName]
           if len(N.shape) != 5:
              sys.exit("Error, ensemble should have five dimensions!")
           nensembles,ntimes,iz,iy,ix = N.shape
           Nsum = np.zeros([1,iy,ix],dtype=np.float32)
           for i in range(0,nensembles):
              Nsum = Nsum + N[i,itime,1,:,:]
           N = np.true_divide(Nsum,nensembles+1)
        elif means == 2:  # Subtract the ensemble mean.
           N = nc.variables[ensembleName]
           if len(N.shape) != 5:
              sys.exit("Error, ensemble should have five dimensions!")
           nensembles,ntimes,iz,iy,ix = N.shape
           N = N[:,0,0:nz,:,:]
           mean = np.mean(N, 0)
           for i in range(0,nensembles): # subtract mean from each ens. member
              N[i,:,:,:] -= mean
        elif means == 3:
           N = nc.variables[ensembleName]
           if len(N.shape) != 5:
              sys.exit("Error, ensemble should have five dimensions!")
           nensembles,ntimes,iz,iy,ix = N.shape
        else:
           sys.exit("Error, bad mean spec!")
 
        if decimate > 1:
           N = N[:,:,::decimate,::decimate]
        gdims = ([ N.shape[1], N.shape[2], N.shape[3] ])
        iLstart,iLend = BTools.range(gdims[2], mpiTasks, mpiRank)
        N = N[:,:,:,iLstart:(iLend+1)]

        nc.close
#       print (mpiRank,": getSlabData: N.shape_final=",N.shape, " nensembles=", nensembles)
#       sys.stdout.flush()
        if len(gdims) == 2:
          gdims = ([1, gdims[0], gdims[1]])
        gdims = ([int(gdims[0]),int(gdims[1]),int(gdims[2]) ])

        return N, nensembles, gdims  # end, getSlabData netghid


    ################################################################
    #  Method: writeResults
    #  Desc  : Writes BMata ressults to a file
    #  Args  : 
    #          B, I, J     : B-matrix entries, and I,J locations in matrix
    #          fileName    : string, filename of the netCDF file to open
    #          mpiRank     : MPI task id
    # Returns: none.
    ################################################################
    @staticmethod
    def writeResults(xB,xI,xJ,filename,mpiRank):

      #
      # Check the inputs.
      #
      if xB.size != xI.size:
         sys.exit("Error, bad size in writeResults!")
      if xI.size != xJ.size:
         sys.exit("Error, bad size in writeResults!")
      if mpiRank < 0:
         sys.exit("Error, bad rank in writeResults!")   


      # Open the netCDF4 file.
      filename = filename + "." + str(mpiRank) + ".nc"
      ncout = Dataset(filename, 'w', formt='NETCDF4')
      
      # Define a dimension for B,I,J.
      nResults = xB.size
      ncout.createDimension('nResults',None)
      
      # Create variables: B,I,J in the file.
      B = ncout.createVariable('B', np.dtype('double').char, ('nResults'))
      I = ncout.createVariable('I', np.dtype('int').char, ('nResults'))
      J = ncout.createVariable('J', np.dtype('int').char, ('nResults'))

      # Write the variables into the file.
      B[:] = xB
      I[:] = xI
      J[:] = xJ

      # Close the file.
      ncout.close()

      # end, method writeResults

